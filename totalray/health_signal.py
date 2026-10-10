"""Passive degradation signals with rolling windows and hysteresis."""
from __future__ import annotations

import re
import subprocess
import threading
import time
from collections import deque


ERROR_PATTERNS = re.compile(
    r"(?:timeout|timed out|connection reset|closed pipe|broken pipe|"
    r"connection refused|unexpected eof|i/o timeout|network is unreachable)",
    re.IGNORECASE,
)


class DegradationDetector:
    """Combine active-tag log errors and retransmit samples safely."""

    def __init__(self, *, error_threshold: int = 3,
                 retransmit_threshold: int = 3,
                 window_seconds: float = 10.0,
                 hysteresis_samples: int = 3):
        self.error_threshold = error_threshold
        self.retransmit_threshold = retransmit_threshold
        self.window_seconds = window_seconds
        self.hysteresis_samples = max(1, hysteresis_samples)
        self._errors: deque[tuple[float, int]] = deque()
        self._retransmits: deque[tuple[float, int]] = deque()
        self._bad_samples = 0
        self._active_tag = ""

    @property
    def active_tag(self) -> str:
        return self._active_tag

    def set_active_tag(self, tag: str | None) -> None:
        tag = tag or ""
        if tag != self._active_tag:
            self._errors.clear()
            self._retransmits.clear()
            self._bad_samples = 0
        self._active_tag = tag

    def observe_log_line(self, line: str, now: float | None = None) -> bool:
        """Record only matching errors that explicitly mention active tag."""
        now = time.time() if now is None else now
        if self._active_tag and self._active_tag in line and ERROR_PATTERNS.search(line):
            self._errors.append((now, 1))
        return self._evaluate(now)

    def observe_retransmits(self, count: int, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        self._retransmits.append((now, max(0, int(count))))
        return self._evaluate(now)

    def observe_connection_errors(self, count: int, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if count:
            self._errors.append((now, int(count)))
        return self._evaluate(now)

    def _evaluate(self, now: float) -> bool:
        cutoff = now - self.window_seconds
        while self._errors and self._errors[0][0] < cutoff:
            self._errors.popleft()
        while self._retransmits and self._retransmits[0][0] < cutoff:
            self._retransmits.popleft()
        bad = (sum(value for _, value in self._errors) >= self.error_threshold or
               any(value >= self.retransmit_threshold for _, value in self._retransmits))
        self._bad_samples = self._bad_samples + 1 if bad else 0
        return self._bad_samples >= self.hysteresis_samples

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        self._evaluate(now)
        return {
            "active_tag": self._active_tag,
            "log_errors": sum(value for _, value in self._errors),
            "retransmit_samples": [value for _, value in self._retransmits],
            "bad_samples": self._bad_samples,
            "degraded": self._bad_samples >= self.hysteresis_samples,
        }


class RetransmitSampler:
    """Best-effort kernel sample; it generates no probe traffic."""

    def __init__(self, runner=subprocess.run):
        self.runner = runner

    def sample(self) -> int:
        try:
            proc = self.runner(["ss", "-tin"], capture_output=True,
                               text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return 0
        total = 0
        for line in proc.stdout.splitlines():
            for pattern in (r"bytes_retrans:(\d+)", r"retrans:(\d+)"):
                match = re.search(pattern, line)
                if match:
                    total += int(match.group(1))
                    break
        return total


class JournalTailer:
    """Read sing-box logs without generating any network traffic."""

    def __init__(self, callback, runner=subprocess.Popen):
        self.callback = callback
        self.runner = runner
        self.process = None
        self.thread = None

    def start(self) -> None:
        try:
            self.process = self.runner(
                ["journalctl", "-u", "sing-box", "-f", "-n", "0", "-o", "cat"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1)
        except (OSError, subprocess.SubprocessError):
            self.process = None
            return
        self.thread = threading.Thread(target=self._read, name="SingboxJournal", daemon=True)
        self.thread.start()

    def _read(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        for line in self.process.stdout:
            self.callback(line.rstrip("\n"))

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                self.process.kill()
        if self.thread:
            self.thread.join(timeout=2)
        self.process = None
        self.thread = None
