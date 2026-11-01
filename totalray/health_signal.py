"""Passive degradation signals with rolling windows and hysteresis."""
from __future__ import annotations

import ipaddress
import logging
import re
import subprocess
import threading
import time
from collections import deque
from typing import NamedTuple

log = logging.getLogger(__name__)

ERROR_PATTERNS = re.compile(
    r"(?:timeout|timed out|connection reset|closed pipe|broken pipe|"
    r"connection refused|unexpected eof|i/o timeout|network is unreachable)",
    re.IGNORECASE,
)


class DegradationDetector:
    """Combine active-tag log errors and retransmit samples safely."""

    def __init__(self, *, error_threshold: int = 3,
                 retransmit_threshold: int = 10,
                 retransmit_ratio: float = 0.15,
                 retransmit_min_segments: int = 100,
                 window_seconds: float = 10.0,
                 hysteresis_samples: int = 3):
        self.error_threshold = error_threshold
        # Retransmits only mean trouble as a share of what was sent: a busy
        # link always retransmits a few segments. Over the window we need
        # at least `retransmit_threshold` retransmitted segments, out of at
        # least `retransmit_min_segments` sent, at `retransmit_ratio` or more.
        self.retransmit_threshold = retransmit_threshold
        self.retransmit_ratio = retransmit_ratio
        self.retransmit_min_segments = retransmit_min_segments
        self.window_seconds = window_seconds
        self.hysteresis_samples = max(1, hysteresis_samples)
        self._errors: deque[tuple[float, int]] = deque()
        self._retransmits: deque[tuple[float, int, int]] = deque()
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

    def observe_retransmits(self, retransmits: int, segments: int,
                            now: float | None = None) -> bool:
        """One sample: segments retransmitted / segments sent since the last."""
        now = time.time() if now is None else now
        with self._lock:
            self._retransmits.append((now, max(0, int(retransmits)), max(0, int(segments))))
            return self._evaluate(now)

    def _retransmit_window(self) -> tuple[int, int]:
        return (sum(r for _, r, _ in self._retransmits),
                sum(g for _, _, g in self._retransmits))

    def _retransmits_bad(self) -> bool:
        retransmitted, sent = self._retransmit_window()
        return (retransmitted >= self.retransmit_threshold
                and sent >= self.retransmit_min_segments
                and retransmitted / sent >= self.retransmit_ratio)

    def _evaluate(self, now: float) -> bool:
        cutoff = now - self.window_seconds
        while self._errors and self._errors[0][0] < cutoff:
            self._errors.popleft()
        while self._retransmits and self._retransmits[0][0] < cutoff:
            self._retransmits.popleft()
        bad = (sum(value for _, value in self._errors) >= self.error_threshold
               or self._retransmits_bad())
        self._bad_samples = self._bad_samples + 1 if bad else 0
        return self._bad_samples >= self.hysteresis_samples

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            self._evaluate(now)
            retransmitted, sent = self._retransmit_window()
            return {
                "active_tag": self._active_tag,
                "log_errors": sum(value for _, value in self._errors),
                "retransmit_samples": [[r, g] for _, r, g in self._retransmits],
                "retransmit_ratio": round(retransmitted / sent, 3) if sent else 0.0,
                "bad_samples": self._bad_samples,
                "degraded": self._bad_samples >= self.hysteresis_samples,
            }


# ss prints one header line per socket (state, queues, local, peer, process),
# then an indented line with the kernel counters.
_SOCKET_HEADER = re.compile(r"^\S+\s+\d+\s+\d+\s+(\S+)\s+(\S+)\s*(.*)$")
# retrans:<in flight>/<total segments retransmitted over the socket's life>
_RETRANS_TOTAL = re.compile(r"\bretrans:\d+/(\d+)")
_SEGS_OUT = re.compile(r"\bsegs_out:(\d+)")
TUNNEL_PROCESSES = ("sing-box", "singbox-core")


def _peer_ip(peer: str):
    """The IP of an `ss` peer column like 1.2.3.4:443 or [2606::1]:443, or None."""
    host = peer.rpartition(":")[0].strip("[]").split("%")[0]
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def _is_public_peer(peer: str) -> bool:
    ip = _peer_ip(peer)
    return ip is not None and not (ip.is_private or ip.is_loopback or ip.is_link_local
                                   or ip.is_unspecified or ip.is_multicast)


def normalize_ips(addresses) -> set[str]:
    """Canonical text for IPs, so '2606:4700::1111' and its long form compare equal."""
    result = set()
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        result.add(str(ip))
    return result


class RetransmitSample(NamedTuple):
    retransmits: int   # segments retransmitted since the previous sample
    segments: int      # segments sent since the previous sample


class RetransmitSampler:
    """Measures TCP retransmits on the tunnel's sockets since the last sample.

    Only sockets owned by sing-box that talk to a public address count;
    pass ``peers`` (the active server's IPs) to look at that server alone.
    Retransmits on LAN-side sockets, on other servers' leftover connections
    or on standby probes say nothing about the active server. The kernel
    keeps lifetime totals per socket, so the first sample only sets the
    baseline and every later sample returns how much the totals grew.
    Every tunnel socket is tracked whether or not it is in ``peers``, so a
    socket never shows up with its whole history after the filter changes.
    Best effort; it generates no probe traffic.
    """

    def __init__(self, runner=subprocess.run, processes=TUNNEL_PROCESSES):
        self.runner = runner
        self.processes = tuple(processes)
        self._seen: dict[tuple[str, str], tuple[int, int]] = {}
        self._primed = False

    def sample(self, peers: set[str] | None = None) -> RetransmitSample:
        try:
            proc = self.runner(["ss", "-tinp"], capture_output=True,
                               text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return RetransmitSample(0, 0)
        wanted = normalize_ips(peers) if peers is not None else None
        current: dict[tuple[str, str], tuple[int, int]] = {}
        in_scope: set[tuple[str, str]] = set()
        key = None
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            if not line[0].isspace():
                key = self._tunnel_socket_key(line)
                if key is not None and (wanted is None or str(_peer_ip(key[1])) in wanted):
                    in_scope.add(key)
                continue
            if key is None:
                continue
            retrans = _RETRANS_TOTAL.search(line)
            segs = _SEGS_OUT.search(line)
            current[key] = (int(retrans.group(1)) if retrans else 0,
                            int(segs.group(1)) if segs else 0)
        retransmitted = sent = 0
        if self._primed:
            for socket_key, (retrans, segs) in current.items():
                if socket_key not in in_scope:
                    continue
                old_retrans, old_segs = self._seen.get(socket_key, (0, 0))
                retransmitted += max(0, retrans - old_retrans)
                sent += max(0, segs - old_segs)
        self._seen = current
        self._primed = True
        return RetransmitSample(retransmitted, sent)

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
