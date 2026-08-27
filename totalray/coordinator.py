"""Centralized sing-box apply coordinator with restart control and recovery.

Phase Six of the concurrency architecture plan:

- All sing-box config writes and restarts go through this coordinator.
- Tracks restart count, failures, reasons, and timestamps.
- Circuit breaker prevents infinite restart loops.
- Health check via Clash API after every restart.
- Status dict available for ``totalray status`` display.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, TYPE_CHECKING

from . import builder

if TYPE_CHECKING:
    from .db import Database

log = logging.getLogger(__name__)

# ── restart reasons ──────────────────────────────────────────────────────
REASON_MEMBERSHIP_CHANGED = "membership_changed"
REASON_RULES_UPDATED = "rules_updated"


def _health_check(settings: Any, retries: int = 3,
                  delay: float = 2.0) -> bool:
    """Probe the Clash API to confirm sing-box is alive after a restart.

    Returns True when the API responds, False after exhausting retries.
    Bounded: at most ``retries * (delay + timeout)`` wall-clock seconds.
    """
    import requests as _requests  # noqa: delayed to avoid circular import
    clash = settings["clash_api"]
    host, _, port = clash["listen"].partition(":")
    host = host or "127.0.0.1"
    secret = clash.get("secret") or ""
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    url = f"http://{host}:{port}/proxies"
    for attempt in range(1, retries + 1):
        try:
            resp = _requests.get(url, headers=headers, timeout=3)
            if resp.status_code in (200, 204):
                return True
        except Exception:  # noqa: BLE001
            pass
        if attempt < retries:
            time.sleep(delay)
    return False


class ApplyCoordinator:
    """Serialize and track every sing-box apply (config write + optional
    restart).  This is the *single* entry point for touching sing-box
    state -- no caller should invoke ``builder.rebuild_and_apply`` or
    ``builder.restart_singbox`` directly.

    Thread-safety: ``apply()`` acquires an internal lock so callers do
    **not** need to hold an external apply lock.
    """

    def __init__(self, settings: Any, db: Database,
                 apply_cfg: dict | None = None):
        self.settings = settings
        self.db = db
        cfg = apply_cfg or settings["apply"]
        self._lock = threading.Lock()

        # ── circuit breaker ──────────────────────────────────────────
        self._max_restarts = int(cfg.get("max_restarts_per_window", 5))
        self._window_seconds = int(cfg.get("restart_window_seconds", 600))
        self._cooldown_seconds = int(cfg.get("restart_cooldown_seconds", 60))
        self._health_retries = int(cfg.get("health_check_retries", 3))
        self._health_delay = float(
            cfg.get("health_check_delay_seconds", 2.0))

        # ── in-memory tracking (also persisted to JSON) ──────────────
        self._restart_timestamps: list[float] = []
        self._restart_count_total = 0
        self._restart_failures_total = 0
        self._last_restart_at: float | None = None
        self._last_restart_reason: str = ""
        self._last_restart_ok = False
        self._circuit_open = False
        self._circuit_open_at: float | None = None

        self._state_path = os.path.join(
            settings["paths"]["data_dir"], "apply_state.json")
        self._load_state()

    # ── public API ──────────────────────────────────────────────────────

    def apply(self, reason: str = REASON_MEMBERSHIP_CHANGED,
              force: bool = False) -> tuple[bool, str]:
        """Apply current pool-B state to sing-box.  Serialised internally.

        Parameters
        ----------
        reason:
            Human-readable reason for logging and status.  Use the
            ``REASON_*`` constants when possible.
        force:
            Skip the tag-comparison fast-path and always do a full
            config rebuild + restart (used after rule-set updates).

        Returns
        -------
        (ok, message) -- identical contract to ``builder.rebuild_and_apply``.
        """
        with self._lock:
            return self._apply_locked(reason=reason, force=force)

    @property
    def status(self) -> dict[str, Any]:
        """Snapshot of coordinator state for ``totalray status`` display."""
        with self._lock:
            self._prune_window()
            return {
                "restarts_total": self._restart_count_total,
                "restart_failures_total": self._restart_failures_total,
                "restarts_in_window": len(self._restart_timestamps),
                "last_restart_at": self._last_restart_at,
                "last_restart_reason": self._last_restart_reason,
                "last_restart_ok": self._last_restart_ok,
                "circuit_open": self._circuit_open,
                "circuit_open_at": self._circuit_open_at,
            }

    def reset_circuit(self) -> None:
        """Manually reset the circuit breaker (e.g. from CLI)."""
        with self._lock:
            self._circuit_open = False
            self._circuit_open_at = None
            self._restart_timestamps.clear()
            self._save_state()
            log.info("apply coordinator circuit breaker reset")

    # ── internals ───────────────────────────────────────────────────────

    def _apply_locked(self, reason: str, force: bool) -> tuple[bool, str]:
        self._prune_window()

        # ── circuit breaker check ────────────────────────────────────
        if self._circuit_open:
            elapsed = time.time() - (self._circuit_open_at or 0)
            if elapsed < self._window_seconds:
                log.warning(
                    "apply coordinator: circuit breaker OPEN "
                    "(%.0fs / %.0fs); skipping apply (reason=%s)",
                    elapsed, self._window_seconds, reason)
                return False, "circuit breaker open -- restarts paused"
            # Window expired -- half-open: allow one attempt
            log.info(
                "apply coordinator: circuit half-open after %.0fs; "
                "allowing one attempt", elapsed)
            self._circuit_open = False


        # ── delegate to builder ──────────────────────────────────────
        # The tag-comparison inside builder.rebuild_and_apply decides
        # whether to do a selector-only switch (no restart) or a full
        # rebuild + restart.  We only gate the *restart* through the
        # circuit breaker.
        try:
            ok, msg = builder.rebuild_and_apply(
                self.settings, self.db, force=force)
        except Exception as exc:  # noqa: BLE001
            log.exception("apply coordinator: unhandled exception")
            self._record_attempt(reason, success=False)
            return False, str(exc)[:400]

        # ── track result ─────────────────────────────────────────────
        # Only count actual restarts (not selector-only switches)
        needs_restart = "(no restart)" not in msg
        if needs_restart:
            self._record_attempt(reason, success=ok)
            if ok:
                # Health check after a successful restart
                healthy = _health_check(
                    self.settings,
                    retries=self._health_retries,
                    delay=self._health_delay)
                if not healthy:
                    log.error(
                        "apply coordinator: sing-box restarted but "
                        "health check FAILED")
                    self._record_attempt(
                        reason + "_health_fail", success=False)
                    return False, (
                        "config written and restarted but health check "
                        "failed -- sing-box may be crash-looping")
        return ok, msg

    def _record_attempt(self, reason: str, success: bool) -> None:
        now = time.time()
        self._restart_count_total += 1
        self._last_restart_at = now
        self._last_restart_reason = reason
        self._last_restart_ok = success
        self._restart_timestamps.append(now)
        if not success:
            self._restart_failures_total += 1
        # Circuit breaker: too many restarts in window
        self._prune_window()
        if len(self._restart_timestamps) >= self._max_restarts:
            self._circuit_open = True
            self._circuit_open_at = now
            log.warning(
                "apply coordinator: circuit breaker OPENED after %d "
                "restarts in %ds window",
                len(self._restart_timestamps), self._window_seconds)
        self._save_state()

    def _prune_window(self) -> None:
        cutoff = time.time() - self._window_seconds
        self._restart_timestamps = [
            ts for ts in self._restart_timestamps if ts > cutoff]

    def _save_state(self) -> None:
        state = {
            "restart_count_total": self._restart_count_total,
            "restart_failures_total": self._restart_failures_total,
            "last_restart_at": self._last_restart_at,
            "last_restart_reason": self._last_restart_reason,
            "last_restart_ok": self._last_restart_ok,
            "circuit_open": self._circuit_open,
            "circuit_open_at": self._circuit_open_at,
        }
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._state_path)),
                        exist_ok=True)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._state_path)
        except OSError as exc:
            log.debug("apply coordinator: could not persist state: %s", exc)

    def _load_state(self) -> None:
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            self._restart_count_total = state.get("restart_count_total", 0)
            self._restart_failures_total = state.get(
                "restart_failures_total", 0)
            self._last_restart_at = state.get("last_restart_at")
            self._last_restart_reason = state.get("last_restart_reason", "")
            self._last_restart_ok = state.get("last_restart_ok", False)
            self._circuit_open = state.get("circuit_open", False)
            self._circuit_open_at = state.get("circuit_open_at")
            # If circuit was open but window has elapsed, clear it
            if self._circuit_open and self._circuit_open_at:
                if time.time() - self._circuit_open_at >= self._window_seconds:
                    self._circuit_open = False
        except (OSError, ValueError, TypeError):
            pass
