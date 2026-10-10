"""Failover policy: cooldown, ready-standby selection, and circuit breaker."""
from __future__ import annotations

import time
from collections import deque
from typing import Callable


class FailoverController:
    def __init__(self, switch: Callable[[str], tuple[bool, str]], *,
                 cooldown_seconds: float = 30.0,
                 max_failures: int = 3,
                 failure_window_seconds: float = 300.0,
                 shadow_mode: bool = True):
        self.switch = switch
        self.cooldown_seconds = cooldown_seconds
        self.max_failures = max_failures
        self.failure_window_seconds = failure_window_seconds
        self.shadow_mode = shadow_mode
        self.last_switch: float | None = None
        self.last_target: str | None = None
        self.history: deque[dict] = deque(maxlen=50)
        self._failures: deque[float] = deque()
        self.circuit_open = False

    def choose(self, ready: list) -> str | None:
        return ready[0].tag if ready else None

    def attempt(self, active_tag: str, ready: list,
                now: float | None = None) -> tuple[bool, str]:
        now = time.time() if now is None else now
        self._prune(now)
        target = self.choose([item for item in ready if item.tag != active_tag])
        if not target:
            return False, "no ready standby"
        if self.circuit_open:
            return False, "failover circuit breaker open"
        if self.last_target == active_tag and self.last_switch is not None:
            # The server promoted by the previous switch is now the one
            # reporting degradation; count that as a failed promotion.
            self.record_promoted_failure(now)
            if self.circuit_open:
                return False, "failover circuit breaker opened after repeated promoted failures"
        if self.last_switch is not None and now - self.last_switch < self.cooldown_seconds:
            return False, "failover cooldown active"
        event = {"at": now, "from": active_tag, "to": target,
                 "shadow": self.shadow_mode, "ok": False}
        if self.shadow_mode:
            event["reason"] = "shadow mode"
            self.history.append(event)
            return False, f"shadow recommendation: {active_tag} -> {target}"
        ok, message = self.switch(target)
        event["ok"] = ok
        event["message"] = message
        self.history.append(event)
        if ok:
            self.last_switch = now
            self.last_target = target
        else:
            self._failures.append(now)
            if len(self._failures) >= self.max_failures:
                self.circuit_open = True
        return ok, message

    def record_promoted_failure(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._failures.append(now)
        self._prune(now)
        if len(self._failures) >= self.max_failures:
            self.circuit_open = True

    def reset_circuit(self) -> None:
        self._failures.clear()
        self.circuit_open = False

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        self._prune(now)
        return {"circuit_open": self.circuit_open,
                "failures_in_window": len(self._failures),
                "last_switch": self.last_switch,
                "last_target": self.last_target,
                "history": list(self.history)}

    def _prune(self, now: float) -> None:
        cutoff = now - self.failure_window_seconds
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()
