"""Safe, bounded health probes for inactive selector members."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import quote

import requests

DEFAULT_URL = "https://www.gstatic.com/generate_204"


@dataclass
class ProbeResult:
    tag: str
    delay_ms: int | None = None
    last_success: float | None = None
    last_error: str | None = None
    # Per-destination outcome of the latest probe: url -> delay in ms, or
    # None when that destination did not answer.
    destinations: dict = field(default_factory=dict)

    def ready(self, now: float, stale_after: float) -> bool:
        return (self.delay_ms is not None and self.delay_ms > 0 and
                self.last_success is not None and now - self.last_success <= stale_after)


class StandbyProbe:
    def __init__(self, host: str, secret: str = "", *, timeout: float = 5.0,
                 stale_after: float = 20.0, session=None,
                 urls: list[str] | None = None, min_ok: int = 1):
        self.host = host
        self.headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        self.timeout = timeout
        self.stale_after = stale_after
        self.session = session or requests.Session()
        # The first URL is the primary one; `min_ok` counts it too and is
        # clamped to the number of URLs. One URL = the original behaviour.
        self.urls = [u for u in (urls or []) if u] or [DEFAULT_URL]
        self.min_ok = max(1, min(int(min_ok), len(self.urls)))
        self._results: dict[str, ProbeResult] = {}
        self._lock = threading.Lock()

    def _probe_url(self, tag: str, url: str) -> tuple[int | None, str | None]:
        endpoint = (f"http://{self.host}/proxies/{quote(tag, safe='')}/delay"
                    f"?url={quote(url, safe='')}&timeout={int(self.timeout * 1000)}")
        try:
            response = self.session.get(endpoint, headers=self.headers, timeout=self.timeout)
            response.raise_for_status()
            delay = int(response.json().get("delay", 0))
            if delay <= 0:
                raise ValueError("probe returned no positive delay")
        except (requests.RequestException, ValueError, TypeError, OSError) as exc:
            return None, str(exc)
        return delay, None

    def probe(self, tag: str, url: str | None = None) -> ProbeResult:
        """Probe ``tag``; ready only if enough destinations answer.

        An explicit ``url`` probes just that one destination (and needs it
        to answer). Otherwise every configured URL is probed in parallel, so
        the call still takes about one timeout, and at least ``min_ok`` of
        them must answer.
        """
        with self._lock:
            result = self._results.setdefault(tag, ProbeResult(tag))
        urls = [url] if url else self.urls
        needed = 1 if url else self.min_ok
        if len(urls) == 1:
            outcomes = [self._probe_url(tag, urls[0])]
        else:
            with ThreadPoolExecutor(max_workers=len(urls)) as pool:
                outcomes = list(pool.map(lambda u: self._probe_url(tag, u), urls))

        destinations = {u: delay for u, (delay, _err) in zip(urls, outcomes)}
        answered = [delay for delay, _err in outcomes if delay]
        errors = [f"{u}: {err}" for u, (_d, err) in zip(urls, outcomes) if err]
        with self._lock:
            result.destinations = destinations
            if len(answered) >= needed:
                # Report the primary destination's latency when it answered.
                result.delay_ms = outcomes[0][0] or min(answered)
                result.last_success = time.time()
                result.last_error = "; ".join(errors) if errors else None
            else:
                result.delay_ms = None
                result.last_error = ("; ".join(errors) if errors
                                     else "not enough destinations answered")
        return result

    def keep_only(self, tags) -> None:
        """Forget servers that are no longer in the group, so the table stays small."""
        keep = set(tags)
        with self._lock:
            for tag in [t for t in self._results if t not in keep]:
                del self._results[tag]

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
                          "destinations": dict(result.destinations),
                          "ready": result.ready(time.time(), self.stale_after)}
                    for tag, result in self._results.items()}
