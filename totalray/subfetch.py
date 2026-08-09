"""Fetch subscriptions and extract config links for TotalRay."""
from __future__ import annotations

import logging
import time


from .parsers import SUPPORTED_SCHEMES, parse_many, is_singbox_config, parse_singbox_config
from . import net
from .http_client import client_for

log = logging.getLogger(__name__)

USER_AGENTS = [
    "v2rayNG/1.9.39",
    "sing-box/1.13.0;SFI",
    "curl/8.5.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
]


def _try_b64_whole(text: str) -> str:
    from .parsers import _b64str
    compact = text.replace("\n", "").replace("\r", "").strip()
    if not compact or len(compact) % 4 == 1:
        return text
    try:
        decoded = _b64str(compact)
    except Exception:  # noqa: BLE001
        return text
    return decoded if any(s in decoded for s in SUPPORTED_SCHEMES) else text


def extract_links(text: str) -> tuple[list, int, list]:
    """Returns (links, skipped_count, skipped_scheme_samples).

    A line only reaches parse_many() if it starts with a scheme we know
    (SUPPORTED_SCHEMES). Anything else - a genuinely different protocol
    (ssr://, socks://, a WireGuard-style sub, ...), a comment/blank line,
    or plain noise - is dropped right here and never shows up in the
    "N invalid" count logged later, because that count only covers links
    we recognized but then failed to parse. skipped_scheme_samples keeps
    a few example prefixes so it's possible to tell whether the dropped
    lines were a real protocol we just don't support yet, or junk.
    """
    text = _try_b64_whole(text)
    links = []
    skipped = 0
    samples: list = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith(SUPPORTED_SCHEMES):
            links.append(line)
            continue
        skipped += 1
        if len(samples) < 5:
            prefix = line.split("://", 1)[0] if "://" in line else line[:20]
            if prefix not in samples:
                samples.append(prefix)
    return links, skipped, samples


def _one_pass(url: str, timeout: int, proxies: dict | None = None,
             header_profiles: list | None = None, client=None):
    profiles = header_profiles or [{"User-Agent": ua, "Accept": "*/*"}
                                    for ua in USER_AGENTS]
    last_exc: Exception | None = None
    for headers in profiles:
        try:
            if client is None:
                # fallback to ad-hoc requests if no client provided
                import requests as _requests
                resp = _requests.get(url, timeout=timeout, proxies=proxies, headers=headers)
            else:
                resp = client.get(url, timeout=timeout, proxies=proxies, headers=headers)
            if resp.status_code == 200 and resp.text.strip():
                return resp.text, None
            last_exc = RuntimeError(f"HTTP {resp.status_code}")
            log.debug("sub %s headers=%s proxy=%s -> HTTP %s",
                     url, headers.get("User-Agent"), bool(proxies), resp.status_code)
        except Exception as exc:
            last_exc = exc
            log.debug("sub %s headers=%s proxy=%s -> error: %s",
                     url, headers.get("User-Agent"), bool(proxies), exc)
    return None, last_exc


def fetch_subscription(settings, url: str, timeout: int = 20, attempts: int = 2,
                       proxy_port: int | None = None,
                       dns_server: str | None = None,
                       extra_headers: dict | None = None) -> str:
    profiles = [extra_headers] if extra_headers else None
    last_exc: Exception | None = None
    client = client_for(settings)
    with net.bypass_tun(dns_server):
        for attempt in range(1, attempts + 1):
            text, exc = _one_pass(url, timeout, header_profiles=profiles, client=client)
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
            text, exc = _one_pass(url, timeout, proxies=proxies, header_profiles=profiles, client=client)
            if text is not None:
                return text
            last_exc = exc
            if attempt < attempts:
                time.sleep(3)

    raise RuntimeError(f"failed to fetch subscription (direct + proxy): {last_exc}")


def update_all(settings, db) -> dict:
    from .settings import Settings
    assert isinstance(settings, Settings)

    for sub in settings.subscriptions:
        db.add_subscription(sub["url"])

    summary = {"subs_ok": 0, "subs_failed": 0, "new_configs": 0, "bad_links": 0}
    proxy_port = int(settings["local_proxy"]["port"])
    dns_server = settings["dns"]["local_server"]
    headers_by_url = {s["url"]: s["headers"] for s in settings.subscriptions if s["headers"]}
    for sub in db.enabled_subscriptions():
        try:
            text = fetch_subscription(
                settings, sub["url"], proxy_port=proxy_port, dns_server=dns_server,
                extra_headers=headers_by_url.get(sub["url"]))
            if is_singbox_config(text):
                # Some panels serve a full sing-box client config instead
                # of a vmess://.../vless://... link list (Hiddify/NekoBox-
                # style). Pull outbounds straight out of it rather than
                # running it through the URI-link pipeline, which would
                # find nothing and misreport the whole subscription as
                # empty/broken.
                items, bad, reasons = parse_singbox_config(text)
                skipped_scheme, scheme_samples = 0, []
                log.info("sub #%s: detected a full sing-box client config"
                         " (not a link list) - extracted outbounds directly",
                         sub["id"])
            else:
                links, skipped_scheme, scheme_samples = extract_links(text)
                items, bad, reasons = parse_many(links)
            added = db.sync_configs(sub["id"], items)
            db.set_sub_status(sub["id"], "ok", len(items))
            summary["subs_ok"] += 1
            summary["new_configs"] += added
            summary["bad_links"] += bad
            log.info("sub #%s: %d links (%d new, %d invalid)",
                     sub["id"], len(items), added, bad)
            if reasons:
                breakdown = ", ".join(
                    f"{cat} x{info['count']} (e.g. {info['example']})"
                    for cat, info in sorted(
                        reasons.items(), key=lambda kv: -kv[1]["count"]))
                log.info("sub #%s: rejected-link breakdown -> %s",
                         sub["id"], breakdown)
            if skipped_scheme:
                log.info(
                    "sub #%s: %d line(s) skipped before parsing - unrecognized"
                    " scheme/protocol, not counted as invalid (samples: %s)",
                    sub["id"], skipped_scheme, ", ".join(scheme_samples) or "-")
        except Exception as exc:  # noqa: BLE001
            db.set_sub_status(sub["id"], f"error: {exc}")
            summary["subs_failed"] += 1
            log.error("error fetching sub #%s: %s", sub["id"], exc)
    return summary
