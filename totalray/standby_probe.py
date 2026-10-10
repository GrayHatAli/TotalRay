"""Safe, bounded health probes for inactive selector members."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests


@dataclass
class ProbeResult:
    tag: str
    delay_ms: int | None = None
    last_success: float | None = None
    last_error: str | None = None

    def ready(self, now: float, stale_after: float) -> bool:
        return (self.delay_ms is not None and self.delay_ms > 0 and
                self.last_success is not None and now - self.last_success <= stale_after)


class StandbyProbe:
    def __init__(self, host: str, secret: str = "", *, timeout: float = 5.0,
                 stale_after: float = 20.0, session=None):
        self.host = host
        self.headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        self.timeout = timeout
        self.stale_after = stale_after
        self.session = session or requests.Session()
        self._results: dict[str, ProbeResult] = {}
        self._lock = threading.Lock()

    def probe(self, tag: str, url: str = "https://www.gstatic.com/generate_204") -> ProbeResult:
        with self._lock:
            result = self._results.setdefault(tag, ProbeResult(tag))
        endpoint = (f"http://{self.host}/proxies/{quote(tag, safe='')}/delay"
                    f"?url={quote(url, safe='')}&timeout={int(self.timeout * 1000)}")
        try:
            response = self.session.get(endpoint, headers=self.headers, timeout=self.timeout)
            response.raise_for_status()
            delay = int(response.json().get("delay", 0))
            if delay <= 0:
                raise ValueError("probe returned no positive delay")
        except (requests.RequestException, ValueError, TypeError, OSError) as exc:
            with self._lock:
                result.delay_ms = None
                result.last_error = str(exc)
        else:
            with self._lock:
                result.delay_ms = delay
                result.last_success = time.time()
                result.last_error = None
        return result

    def ready(self, tags: list[str], now: float | None = None) -> list[ProbeResult]:
        now = time.time() if now is None else now
        with self._lock:
            return sorted((self._results[tag] for tag in tags if tag in self._results
                           and self._results[tag].ready(now, self.stale_after)),
                          key=lambda item: item.delay_ms or 2**31)

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {tag: {"tag": result.tag, "delay_ms": result.delay_ms,
                          "last_success": result.last_success,
                          "last_error": result.last_error,
                          "ready": result.ready(time.time(), self.stale_after)}
                    for tag, result in self._results.items()}
