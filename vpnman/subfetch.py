"""Fetch subscriptions and extract config links."""
from __future__ import annotations

import logging
import time

import requests

from .parsers import SUPPORTED_SCHEMES, parse_many
from . import net

log = logging.getLogger(__name__)

# Some subscription servers are picky about User-Agent; try these in order.
USER_AGENTS = [
    "v2rayNG/1.9.39",
    "sing-box/1.13.0;SFI",
    "curl/8.5.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
]


def _try_b64_whole(text: str) -> str:
    """If the whole body is base64, decode it; otherwise return it unchanged."""
    from .parsers import _b64str  # internal reuse
    compact = text.replace("\n", "").replace("\r", "").strip()
    if not compact or len(compact) % 4 == 1:
        return text
    try:
        decoded = _b64str(compact)
    except Exception:  # noqa: BLE001
        return text
    return decoded if any(s in decoded for s in SUPPORTED_SCHEMES) else text


def extract_links(text: str) -> list:
    text = _try_b64_whole(text)
    links = []
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith(SUPPORTED_SCHEMES):
            links.append(line)
    return links


def _one_pass(url: str, timeout: int, proxies: dict | None = None,
             header_profiles: list | None = None):
    """Try each header profile once. Returns (text, None) or (None, last_exc).

    Default profiles just rotate User-Agent. A subscription can instead
    supply its own exact header set (see Settings.subscriptions) for
    panels that check for a specific app's fingerprint rather than just
    "does this look like some known VPN client".
    """
    profiles = header_profiles or [{"User-Agent": ua, "Accept": "*/*"}
                                    for ua in USER_AGENTS]
    last_exc: Exception | None = None
    for headers in profiles:
        try:
            resp = requests.get(
                url, timeout=timeout, proxies=proxies, headers=headers)
            if resp.status_code == 200 and resp.text.strip():
                return resp.text, None
            last_exc = RuntimeError(f"HTTP {resp.status_code}")
            log.debug("sub %s headers=%s proxy=%s -> HTTP %s",
                     url, headers.get("User-Agent"), bool(proxies), resp.status_code)
        except requests.RequestException as exc:
            last_exc = exc
            log.debug("sub %s headers=%s proxy=%s -> error: %s",
                     url, headers.get("User-Agent"), bool(proxies), exc)
    return None, last_exc


def fetch_subscription(url: str, timeout: int = 20, attempts: int = 2,
                       proxy_port: int | None = None,
                       dns_server: str | None = None,
                       extra_headers: dict | None = None) -> str:
    """Direct first (bypassing the TUN entirely); proxy as an explicit
    fallback if direct genuinely fails.

    "Direct" here means every socket opened during the attempt - including
    a manual DNS-over-UDP lookup done via `dns_server`, see net.py for why
    the normal resolver can't just be marked - is tagged with sing-box's
    own auto_redirect_output_mark. That is the exact mechanism sing-box
    uses to keep its own outbound connections from looping back into its
    own TUN - reusing it means vpnman's subscription fetching genuinely
    never depends on the tunnel it is itself building, instead of
    silently going through it and hoping the currently active server is
    healthy.

    If direct still fails (the host is only reachable through a proxy in
    the first place, e.g. a GitHub raw URL from a network where it's
    blocked outright), fall back to the explicit local SOCKS proxy.

    `extra_headers`, if given, replaces the default User-Agent rotation
    with exactly this one header set - for panels that reject anything
    that isn't a byte-for-byte match of one specific app's request (see
    Settings.subscriptions docstring).
    """
    profiles = [extra_headers] if extra_headers else None
    last_exc: Exception | None = None
    with net.bypass_tun(dns_server):
        for attempt in range(1, attempts + 1):
            text, exc = _one_pass(url, timeout, header_profiles=profiles)
            if text is not None:
                return text
            last_exc = exc
            if attempt < attempts:
                log.info("sub %s: direct attempt %d/%d failed, retrying...",
                         url, attempt, attempts)
                time.sleep(3)

    if proxy_port:
        log.info("sub %s: direct failed, retrying explicitly through"
                 " the local proxy (127.0.0.1:%d)...", url, proxy_port)
        proxies = {"http": f"socks5h://127.0.0.1:{proxy_port}",
                   "https": f"socks5h://127.0.0.1:{proxy_port}"}
        for attempt in range(1, attempts + 1):
            text, exc = _one_pass(url, timeout, proxies=proxies, header_profiles=profiles)
            if text is not None:
                return text
            last_exc = exc
            if attempt < attempts:
                time.sleep(3)

    raise RuntimeError(f"failed to fetch subscription (direct + proxy): {last_exc}")


def update_all(settings, db) -> dict:
    """Fetch, parse, and merge every enabled subscription into the database."""
    from .settings import Settings  # avoid circular import in type checks
    assert isinstance(settings, Settings)

    # sync the YAML list -> database
    for sub in settings.subscriptions:
        db.add_subscription(sub["url"])

    summary = {"subs_ok": 0, "subs_failed": 0, "new_configs": 0, "bad_links": 0}
    proxy_port = int(settings["local_proxy"]["port"])
    dns_server = settings["dns"]["local_server"]
    headers_by_url = {s["url"]: s["headers"] for s in settings.subscriptions if s["headers"]}
    for sub in db.enabled_subscriptions():
        try:
            text = fetch_subscription(
                sub["url"], proxy_port=proxy_port, dns_server=dns_server,
                extra_headers=headers_by_url.get(sub["url"]))
            links = extract_links(text)
            items, bad = parse_many(links)
            added = db.sync_configs(sub["id"], items)
            db.set_sub_status(sub["id"], "ok", len(items))
            summary["subs_ok"] += 1
            summary["new_configs"] += added
            summary["bad_links"] += bad
            log.info("sub #%s: %d links (%d new, %d invalid)",
                     sub["id"], len(items), added, bad)
        except Exception as exc:  # noqa: BLE001
            db.set_sub_status(sub["id"], f"error: {exc}")
            summary["subs_failed"] += 1
            log.error("error fetching sub #%s: %s", sub["id"], exc)
    return summary
