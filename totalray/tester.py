"""Group latency testing for TotalRay."""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import net

log = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 96
PORT_WAIT_SECONDS = 8.0
# Extra-destination checks give up sooner than the primary test.
EXTRA_URL_TIMEOUT_SECONDS = 6


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _wait_ports(ports, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    remaining = set(ports)
    while time.monotonic() < deadline and remaining:
        for port in list(remaining):
            if _port_open(port):
                remaining.discard(port)
        if remaining:
            time.sleep(0.15)
    return not remaining


def build_test_config(items: list, base_port: int, dns_server: str) -> dict:
    # This test instance's own outbound TCP dials are locally-originated
    # traffic on the same box that runs the main gateway's sing-box, which
    # transparently redirects local TCP connections back through itself
    # unless they carry REDIRECT_OUTPUT_MARK (see net.py / auto_redirect
    # nftables rules -- UDP/ICMP gets an automatic bypass for local
    # traffic, TCP does not). Without this, every single connectivity
    # check gets silently double-hopped through the gateway's own tun,
    # adding latency/CPU and sometimes breaking the proxy handshake
    # entirely -- which was masquerading as "candidate unreachable".
    # routing_mark tells sing-box's Go dialer to set the same SO_MARK
    # Python's bypass_tun()/internet_up() use, so these dials correctly
    # bypass the gateway's redirect instead of looping back through it.
    inbounds, outbounds, rules = [], [], []
    for idx, item in enumerate(items):
        itag, otag = f"t-{idx}", f"o-{idx}"
        inbounds.append({"type": "mixed", "tag": itag,
                         "listen": "127.0.0.1", "listen_port": base_port + idx})
        ob = dict(item["outbound"])
        ob["tag"] = otag
        ob["routing_mark"] = net.REDIRECT_OUTPUT_MARK_INT
        outbounds.append(ob)
        rules.append({"inbound": itag, "action": "route", "outbound": otag})
    outbounds.append({"type": "direct", "tag": "direct",
                      "routing_mark": net.REDIRECT_OUTPUT_MARK_INT})
    return {
        "log": {"level": "error"},
        "dns": {"servers": [{"type": "udp", "tag": "local",
                             "server": dns_server}],
                "final": "local"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "route": {"rules": rules, "final": "direct",
                  "auto_detect_interface": True,
                  "default_domain_resolver": "local"},
    }


def _measure_once(port: int, url: str, timeout: int) -> int:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{time_total}",
             "--max-time", str(timeout),
             "-x", f"socks5h://127.0.0.1:{port}", url],
            capture_output=True, text=True, timeout=timeout + 5)
    except (subprocess.SubprocessError, OSError):
        return -1
    if proc.returncode != 0:
        return -1
    try:
        return max(1, int(float(proc.stdout.strip()) * 1000))
    except ValueError:
        return int((time.monotonic() - started) * 1000)


def measure(port: int, url: str, timeout: int, retries: int) -> int:
    for attempt in range(retries + 1):
        delay = _measure_once(port, url, timeout)
        if delay > 0:
            return delay
        if attempt < retries:
            time.sleep(0.4)
    return -1


def measure_multi(port: int, url: str, extra_urls, timeout: int, retries: int,
                  min_ok: int) -> int:
    """Primary-URL delay, but only if enough destinations answer.

    The primary URL runs first and a failure there returns -1 at once, so
    servers that are simply dead cost nothing extra. The extras run in order
    only for servers that passed it, and stop as soon as the threshold is met
    (or can no longer be met). ``min_ok`` counts the primary URL too and is
    clamped to the number of URLs configured. With no extras this is exactly
    ``measure``.
    """
    delay = measure(port, url, timeout, retries)
    extra_urls = list(extra_urls or [])
    if delay <= 0 or not extra_urls:
        return delay
    required = max(1, min(int(min_ok), 1 + len(extra_urls)))
    extra_timeout = min(int(timeout), EXTRA_URL_TIMEOUT_SECONDS)
    passed = 1
    for index, extra in enumerate(extra_urls):
        if passed >= required:
            break
        if passed + (len(extra_urls) - index) < required:
            return -1
        # No retry and a shorter timeout: one flaky extra is already
        # tolerated by the majority rule, so retrying it only slows rounds.
        if measure(port, extra, extra_timeout, 0) > 0:
            passed += 1
    return delay if passed >= required else -1


class GroupTester:
    def __init__(self, settings, pool: str = "a"):
        self.bin = settings["paths"]["sing_box_bin"]
        test = settings["test"]
        self.url = test["url"]
        # Extra destinations (see measure_multi). Empty = single-URL check.
        self.extra_urls = list(test.get("extra_urls") or [])
        self.min_destinations_ok = int(test.get("min_destinations_ok", 2))
        self.timeout = int(test["timeout_seconds"])
        self.concurrency = int(test["concurrency"])
        self.retries = int(test["retries"])
        self.chunk_size = int(test.get("chunk_size", DEFAULT_CHUNK_SIZE))
        # Pool A and Pool B rounds run at the same time, and each test
        # sing-box binds one local port per config. Give Pool B its own
        # range right after Pool A's so the two never share a port.
        offset = self.chunk_size if pool == "b" else 0
        self.base_port = int(test["base_port"]) + offset
        self.dns_server = settings["dns"]["local_server"]

    def _check_items(self, items: list) -> tuple[bool, str]:
        """Validate a group of outbounds with sing-box without starting it."""
        cfg = build_test_config(items, self.base_port, self.dns_server)
        fd, path = tempfile.mkstemp(prefix="totalray-test-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(cfg, fh)
            check = subprocess.run([self.bin, "check", "-c", path],
                                   capture_output=True, text=True, timeout=60)
            return check.returncode == 0, (check.stderr or check.stdout)[-500:]
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _find_valid_items(self, items: list) -> list:
        """Keep valid outbounds when one item poisons a whole batch.

        sing-box validates the complete configuration at once. Splitting a
        rejected batch recursively lets us identify malformed entries while
        retaining valid entries for connectivity testing.
        """
        if not items:
            return []
        valid, detail = self._check_items(items)
        if valid:
            return items
        if len(items) == 1:
            log.warning("skipping invalid config %s: %s",
                        items[0]["id"], detail)
            return []
        midpoint = len(items) // 2
        return (self._find_valid_items(items[:midpoint])
                + self._find_valid_items(items[midpoint:]))

    def _run_chunk(self, items: list) -> dict:
        results = {item["id"]: -1 for item in items}
        valid, detail = self._check_items(items)
        if not valid:
            log.error("test config rejected by sing-box: %s", detail)
            items = self._find_valid_items(items)
            if not items:
                return results
            log.info("continuing with %d valid configs from rejected batch",
                     len(items))

        cfg = build_test_config(items, self.base_port, self.dns_server)
        ports = [self.base_port + i for i in range(len(items))]
        # A port that already answers belongs to someone else (another test
        # round, a leftover process). Starting anyway would make us measure
        # the wrong sing-box and score configs by someone else's results.
        busy = [p for p in ports if _port_open(p)]
        if busy:
            raise RuntimeError(
                f"test ports already in use: {busy[:5]}"
                f"{' ...' if len(busy) > 5 else ''}")
        fd, path = tempfile.mkstemp(prefix="totalray-test-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(cfg, fh)
            proc = subprocess.Popen([self.bin, "run", "-c", path],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            try:
                if not _wait_ports(ports, PORT_WAIT_SECONDS):
                    log.warning("some test ports never came up")
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"test sing-box exited during startup "
                        f"(code {proc.returncode})")
                with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                    futures = {
                        pool.submit(measure_multi, self.base_port + i,
                                    self.url, self.extra_urls, self.timeout,
                                    self.retries, self.min_destinations_ok):
                        item["id"]
                        for i, item in enumerate(items)
                    }
                    for fut in as_completed(futures):
                        results[futures[fut]] = fut.result()
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return results

    def test_all(self, items: list, on_chunk=None) -> dict:
        """Test items chunk by chunk and optionally publish each result.

        ``on_chunk`` receives ``(chunk_items, chunk_results)`` immediately
        after each chunk finishes. This lets callers persist successful
        configs without waiting for a very large pool to finish.
        """
        results = {}
        total = len(items)
        for offset in range(0, total, self.chunk_size):
            chunk = items[offset:offset + self.chunk_size]
            log.info("testing batch %d-%d of %d configs...",
                     offset + 1, offset + len(chunk), total)
            chunk_results = self._run_chunk(chunk)
            results.update(chunk_results)
            if on_chunk is not None:
                on_chunk(chunk, chunk_results)
            ok = sum(1 for v in results.values() if v > 0)
            log.info("progress: %d/%d reachable", ok, len(results))
        return results
