"""Group latency testing for configs.

Instead of ping, a real HTTPS request is made "through the tunnel" for
each config, measuring the full response time (handshake + round-trip) -
exactly what the user experiences in practice.

Implementation: a temporary sing-box instance with N mixed inbounds on
localhost (one port per config) and a static inbound->outbound route,
then curl requests fired concurrently against each port.
Output: {config_id: delay_ms | -1}
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 96      # max inbounds per temporary sing-box instance
PORT_WAIT_SECONDS = 8.0


def _wait_ports(ports, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    remaining = set(ports)
    while time.monotonic() < deadline and remaining:
        for port in list(remaining):
            with socket.socket() as sock:
                sock.settimeout(0.25)
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    remaining.discard(port)
        if remaining:
            time.sleep(0.15)
    return not remaining


def build_test_config(items: list, base_port: int, dns_server: str) -> dict:
    inbounds, outbounds, rules = [], [], []
    for idx, item in enumerate(items):
        itag, otag = f"t-{idx}", f"o-{idx}"
        inbounds.append({"type": "mixed", "tag": itag,
                         "listen": "127.0.0.1", "listen_port": base_port + idx})
        ob = dict(item["outbound"])
        ob["tag"] = otag
        outbounds.append(ob)
        rules.append({"inbound": itag, "action": "route", "outbound": otag})
    outbounds.append({"type": "direct", "tag": "direct"})
    return {
        "log": {"level": "error"},
        # NOTE: no "detour" on this DNS server. sing-box >= 1.12 rejects a
        # DNS server that detours to a bare/empty "direct" outbound with
        # "detour to an empty direct outbound makes no sense". Omitting
        # detour is equivalent here anyway, since direct routing is the
        # only outbound available in this throwaway test config.
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
    """Total time for one real request through the port (ms), or -1."""
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


class GroupTester:
    def __init__(self, settings):
        self.bin = settings["paths"]["sing_box_bin"]
        test = settings["test"]
        self.url = test["url"]
        self.timeout = int(test["timeout_seconds"])
        self.concurrency = int(test["concurrency"])
        self.retries = int(test["retries"])
        self.base_port = int(test["base_port"])
        self.chunk_size = int(test.get("chunk_size", DEFAULT_CHUNK_SIZE))
        self.dns_server = settings["dns"]["local_server"]

    def _run_chunk(self, items: list) -> dict:
        cfg = build_test_config(items, self.base_port, self.dns_server)
        results = {item["id"]: -1 for item in items}
        fd, path = tempfile.mkstemp(prefix="vpnman-test-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(cfg, fh)
            check = subprocess.run([self.bin, "check", "-c", path],
                                   capture_output=True, text=True, timeout=60)
            if check.returncode != 0:
                log.error("test config rejected by sing-box: %s",
                          (check.stderr or check.stdout)[-500:])
                return results
            proc = subprocess.Popen([self.bin, "run", "-c", path],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            try:
                ports = [self.base_port + i for i in range(len(items))]
                if not _wait_ports(ports, PORT_WAIT_SECONDS):
                    log.warning("some test ports never came up")
                with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                    futures = {
                        pool.submit(measure, self.base_port + i,
                                    self.url, self.timeout, self.retries):
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

    def test_all(self, items: list) -> dict:
        """items: [{'id':..., 'outbound':{...}}] -> {id: delay_ms | -1}"""
        results = {}
        total = len(items)
        for offset in range(0, total, self.chunk_size):
            chunk = items[offset:offset + self.chunk_size]
            log.info("testing batch %d-%d of %d configs...",
                     offset + 1, offset + len(chunk), total)
            results.update(self._run_chunk(chunk))
            ok = sum(1 for v in results.values() if v > 0)
            log.info("progress: %d/%d reachable", ok, len(results))
        return results
