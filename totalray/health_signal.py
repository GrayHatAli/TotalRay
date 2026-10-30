"""Passive degradation signals with rolling windows and hysteresis."""
from __future__ import annotations

import ipaddress
import logging
import re
import subprocess
import threading
import time
from collections import deque

log = logging.getLogger(__name__)

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
        # The journal thread feeds log lines while the monitor thread feeds
        # connection and retransmit samples; both mutate the same deques.
        self._lock = threading.RLock()

    @property
    def active_tag(self) -> str:
        return self._active_tag

    def set_active_tag(self, tag: str | None) -> None:
        tag = tag or ""
        with self._lock:
            if tag != self._active_tag:
                self._errors.clear()
                self._retransmits.clear()
                self._bad_samples = 0
            self._active_tag = tag

    def observe_log_line(self, line: str, now: float | None = None) -> bool:
        """Record only matching errors that explicitly mention active tag."""
        now = time.time() if now is None else now
        with self._lock:
            if self._active_tag and self._active_tag in line and ERROR_PATTERNS.search(line):
                self._errors.append((now, 1))
            return self._evaluate(now)

    def observe_retransmits(self, count: int, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            self._retransmits.append((now, max(0, int(count))))
            return self._evaluate(now)

    def observe_connection_errors(self, count: int, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
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
        with self._lock:
            self._evaluate(now)
            return {
                "active_tag": self._active_tag,
                "log_errors": sum(value for _, value in self._errors),
                "retransmit_samples": [value for _, value in self._retransmits],
                "bad_samples": self._bad_samples,
                "degraded": self._bad_samples >= self.hysteresis_samples,
            }


# ss prints one header line per socket (state, queues, local, peer, process),
# then an indented line with the kernel counters.
_SOCKET_HEADER = re.compile(r"^\S+\s+\d+\s+\d+\s+(\S+)\s+(\S+)\s*(.*)$")
# retrans:<in flight>/<total segments retransmitted over the socket's life>
_RETRANS_TOTAL = re.compile(r"\bretrans:\d+/(\d+)")
TUNNEL_PROCESSES = ("sing-box", "singbox-core")


def _is_public_peer(peer: str) -> bool:
    host = peer.rpartition(":")[0].strip("[]").split("%")[0]
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_unspecified or ip.is_multicast)


class RetransmitSampler:
    """Counts TCP segments retransmitted since the previous sample.

    Only sockets owned by sing-box that talk to a public address count:
    those are the tunnel's own connections. Retransmits on LAN-side or
    unrelated sockets say nothing about the active server. The kernel
    keeps a lifetime total per socket, so the first sample only sets the
    baseline and every later sample returns how much the totals grew.
    Best effort; it generates no probe traffic.
    """

    def __init__(self, runner=subprocess.run, processes=TUNNEL_PROCESSES):
        self.runner = runner
        self.processes = tuple(processes)
        self._seen: dict[tuple[str, str], int] = {}
        self._primed = False

    def sample(self) -> int:
        try:
            proc = self.runner(["ss", "-tinp"], capture_output=True,
                               text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return 0
        current: dict[tuple[str, str], int] = {}
        key = None
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            if not line[0].isspace():
                key = self._tunnel_socket_key(line)
                continue
            if key is None:
                continue
            match = _RETRANS_TOTAL.search(line)
            current[key] = int(match.group(1)) if match else 0
        grown = 0
        if self._primed:
            for socket_key, total in current.items():
                grown += max(0, total - self._seen.get(socket_key, 0))
        self._seen = current
        self._primed = True
        return grown

    def _tunnel_socket_key(self, line: str):
        match = _SOCKET_HEADER.match(line)
        if not match:
            return None
        local, peer, rest = match.groups()
        if not any(f'"{name}"' in rest for name in self.processes):
            return None
        if not _is_public_peer(peer):
            return None
        return (local, peer)


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
            try:
                self.callback(line.rstrip("\n"))
            except Exception:  # noqa: BLE001
                # One bad line must not end the passive log signal for good.
                log.exception("journal line handler failed")

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
