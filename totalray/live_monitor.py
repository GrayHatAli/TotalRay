"""Live monitor for TotalRay: passive degradation signals and failover.

Watches the currently selected sing-box outbound, detects degradation
from the sing-box log and TCP retransmits, and fails over to a ready
standby server.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Callable

import requests

from .failover import FailoverController
from .health_signal import DegradationDetector, JournalTailer, RetransmitSampler
from .standby_probe import StandbyProbe

log = logging.getLogger(__name__)


class LiveMonitor:
    """Monitor live connection quality and trigger failover on degradation.

    This runs continuously in the background and watches the active
    outbound through two passive signals (no probe traffic on live
    connections):
    - error lines for the active server in the sing-box log
    - TCP retransmit growth on the tunnel's own sockets

    It also probes the standby servers through the Clash API. When the
    active server degrades it switches to the best ready standby (or only
    logs the decision while ``shadow_mode`` is on).
    """

    def __init__(
        self,
        settings,
        db,
        check_interval: float = 2.0,
        error_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        hysteresis_samples: int = 3,
        error_window_seconds: float = 10.0,
        retransmit_threshold: int = 3,
        retransmit_sample_seconds: float = 5.0,
        probe_interval_seconds: float = 7.0,
        probe_timeout_seconds: float = 5.0,
        probe_stale_seconds: float = 20.0,
        circuit_breaker_failures: int = 3,
        circuit_breaker_window_seconds: float = 300.0,
        shadow_mode: bool = True,
    ):
        self.settings = settings
        self.db = db
        self.check_interval = check_interval
        self.error_threshold = error_threshold
        self.cooldown_seconds = cooldown_seconds
        self.retransmit_sample_seconds = retransmit_sample_seconds
        self.probe_interval_seconds = probe_interval_seconds

        self._clash_host = settings.data["clash_api"]["listen"]
        self._clash_secret = settings.data["clash_api"].get("secret", "")
        self._headers = {}
        if self._clash_secret:
            self._headers["Authorization"] = f"Bearer {self._clash_secret}"

        self._running = False
        self._thread: threading.Thread | None = None
        self._last_failover = 0.0
        self._failover_count = 0
        self._on_failover: Callable[[str], None] | None = None
        self._started_at = 0.0
        self._last_status_write = 0.0
        data_dir = getattr(settings, "data_dir", "/var/lib/totalray")
        self._status_path = os.path.join(data_dir, "live_monitor_status.json")
        self._signal = DegradationDetector(
            error_threshold=error_threshold,
            retransmit_threshold=retransmit_threshold,
            window_seconds=error_window_seconds,
            hysteresis_samples=hysteresis_samples,
        )
        self._retransmit_sampler = RetransmitSampler()
        self._last_retransmit_sample = 0.0
        self._last_probe = 0.0
        self._probe = StandbyProbe(
            self._clash_host, self._clash_secret,
            timeout=probe_timeout_seconds, stale_after=probe_stale_seconds)
        self._failover = FailoverController(
            self._do_failover,
            cooldown_seconds=cooldown_seconds,
            max_failures=circuit_breaker_failures,
            failure_window_seconds=circuit_breaker_window_seconds,
            shadow_mode=shadow_mode,
        )
        self._journal = JournalTailer(self.feed_log_line)
        self._last_degraded_tag: str | None = None

    def _write_status(self, extra: dict | None = None) -> None:
        """Write a small JSON snapshot to disk so a separate short-lived
        CLI process (totalray status) can report on this thread's
        state without needing IPC into the running daemon.
        """
        now = time.time()
        payload = {
            "running": self._running,
            "started_at": self._started_at or None,
            "last_check": now,
            "failover_count": self._failover_count,
            "last_failover": self._last_failover or None,
            "check_interval": self.check_interval,
            "passive_signal": self._signal.snapshot(),
            "standbys": self._probe.snapshot(),
            "failover": self._failover.snapshot(),
        }
        if extra:
            payload.update(extra)
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(self._status_path) or ".", prefix=".lm-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            # tempfile.mkstemp always creates the file mode 0600 regardless of
            # umask; match the rest of /var/lib/totalray (e.g. totalray.db,
            # totalray.log) which are group-readable, so status checks work
            # for anyone in the totalray group, not just root/sudo.
            os.chmod(tmp_path, 0o664)
            os.replace(tmp_path, self._status_path)
        except OSError as exc:
            log.debug("could not write live-monitor status file: %s", exc)
        self._last_status_write = now

    def set_failover_callback(self, callback: Callable[[str], None]) -> None:
        """Set callback to be called when failover occurs.

        The callback receives the tag of the new active server.
        """
        self._on_failover = callback

    def feed_log_line(self, line: str) -> None:
        """Feed one daemon log line; useful for tests and journal tailing."""
        self._signal.observe_log_line(line)

    @property
    def probe_status(self) -> dict:
        return self._probe.snapshot()

    @property
    def failover_status(self) -> dict:
        return self._failover.snapshot()

    @property
    def last_degraded_tag(self) -> str | None:
        return self._last_degraded_tag

    def _api_get(self, endpoint: str) -> dict | None:
        """Make a GET request to the Clash API."""
        url = f"http://{self._clash_host}{endpoint}"
        try:
            resp = requests.get(url, headers=self._headers, timeout=3)
            if resp.status_code == 200:
                return resp.json()
        except (requests.RequestException, OSError, ValueError) as exc:
            log.debug("Clash API error (%s): %s", endpoint, exc)
        return None

    def _get_proxy_info(self) -> dict | None:
        """Get current proxy selection info from Clash API."""
        data = self._api_get("/proxies")
        if not data:
            return None

        proxies = data.get("proxies", {})
        auto_proxy = proxies.get("auto", {})
        if not auto_proxy:
            return None

        return {
            "now": auto_proxy.get("now"),
            "all": auto_proxy.get("all", []),
        }

    def _probe_standbys(self, active_tag: str) -> None:
        for cfg in self.db.get_pool_configs("b", max_n=10):
            tag = f"cfg-{cfg['id']}"
            if tag != active_tag:
                self._probe.probe(tag)

    def _sample_retransmits(self, now: float) -> None:
        if now - self._last_retransmit_sample < self.retransmit_sample_seconds:
            return
        self._last_retransmit_sample = now
        self._signal.observe_retransmits(self._retransmit_sampler.sample(), now)

    def _do_failover(self, new_tag: str) -> tuple[bool, str]:
        """Switch to a new server via Clash API.

        Returns ``(ok, message)``, the shape FailoverController expects.
        """
        from . import builder

        ok, msg = builder.set_active_config(self.settings, new_tag)
        if ok:
            now = time.time()
            self._last_failover = now
            self._failover_count += 1
            log.warning(
                "LIVE FAILOVER: switched from previous server to %s "
                "(failover #%d)",
                new_tag,
                self._failover_count,
            )
            self._write_status({"last_target": new_tag})
            if self._on_failover:
                # The switch already happened; a failing refill callback
                # must not make it look like the switch failed.
                try:
                    self._on_failover(new_tag)
                except Exception:  # noqa: BLE001
                    log.exception("post-failover callback failed")
            return True, f"switched to {new_tag}"
        log.error("Live failover to %s failed: %s", new_tag, msg)
        self._write_status({"last_failover_error": msg})
        return False, msg

    def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        log.info("Live connection monitor started (interval=%ss)", self.check_interval)

        while self._running:
            try:
                if time.time() - self._last_status_write >= 10:
                    self._write_status()

                # Get current server
                proxy_info = self._get_proxy_info()
                if not proxy_info:
                    time.sleep(self.check_interval)
                    continue

                current_tag = proxy_info.get("now")
                if not current_tag:
                    time.sleep(self.check_interval)
                    continue

                self._signal.set_active_tag(current_tag)
                now = time.time()
                self._sample_retransmits(now)
                if now - self._last_probe >= self.probe_interval_seconds:
                    self._last_probe = now
                    self._probe_standbys(current_tag)

                # Degradation comes from the sing-box log and the retransmit
                # sampler (both feed self._signal); the open-connection list
                # cannot tell a failed connection from a new one.
                if self._signal.snapshot().get("degraded", False):
                    self._last_degraded_tag = current_tag
                    ready = self._probe.ready(
                        [f"cfg-{cfg['id']}" for cfg in self.db.get_pool_configs("b", max_n=10)
                         if f"cfg-{cfg['id']}" != current_tag])
                    ok, message = self._failover.attempt(current_tag, ready)
                    if ok:
                        log.warning("automatic failover completed: %s", message)
                    elif message.startswith("shadow"):
                        log.warning("%s", message)
                    elif message not in ("no ready standby", "failover cooldown active"):
                        log.warning("failover not performed: %s", message)

                time.sleep(self.check_interval)

            except Exception as exc:
                log.exception("Error in monitor loop: %s", exc)
                time.sleep(self.check_interval)

        self._write_status()
        log.info("Live connection monitor stopped")

    def start(self) -> None:
        """Start the monitoring thread."""
        if self._running:
            return
        self._running = True
        self._started_at = time.time()
        self._write_status()
        self._journal.start()
        self._thread = threading.Thread(
            target=self._monitor_loop, name="LiveMonitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the monitoring thread."""
        self._running = False
        self._journal.stop()
        self._write_status()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

def create_monitor(settings, db) -> LiveMonitor:
    """Create a LiveMonitor instance with configured settings."""
    lm_cfg = settings.data.get("live_monitor", {})
    
    # Use tighter thresholds for streaming-sensitive use cases
    return LiveMonitor(
        settings=settings,
        db=db,
        check_interval=float(lm_cfg.get("check_interval_seconds", 2.0)),
        error_threshold=int(lm_cfg.get("error_threshold", 3)),
        cooldown_seconds=float(lm_cfg.get("cooldown_seconds", 60.0)),
        hysteresis_samples=int(lm_cfg.get("hysteresis_samples", 3)),
        error_window_seconds=float(lm_cfg.get("error_window_seconds", 10.0)),
        retransmit_threshold=int(lm_cfg.get("retransmit_threshold", 3)),
        retransmit_sample_seconds=float(lm_cfg.get("retransmit_sample_seconds", 5.0)),
        probe_interval_seconds=float(lm_cfg.get("probe_interval_seconds", 7.0)),
        probe_timeout_seconds=float(lm_cfg.get("probe_timeout_seconds", 5.0)),
        probe_stale_seconds=float(lm_cfg.get("probe_stale_seconds", 20.0)),
        circuit_breaker_failures=int(lm_cfg.get("circuit_breaker_failures", 3)),
        circuit_breaker_window_seconds=float(lm_cfg.get("circuit_breaker_window_seconds", 300.0)),
        shadow_mode=bool(lm_cfg.get("shadow_mode", True)),
    )
