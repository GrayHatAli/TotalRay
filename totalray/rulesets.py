"""Download and update the Iran rule-sets (sing-box binary .srs format) for TotalRay."""
from __future__ import annotations

import logging
import os

from .http_client import client_for
from . import net

log = logging.getLogger(__name__)

_RULESET_MIRRORS = {
    "geosite-ir": [
        "https://raw.githubusercontent.com/Chocolate4U/Iran-sing-box-rules/rule-set/geosite-ir.srs",
        "https://cdn.jsdelivr.net/gh/chocolate4u/Iran-sing-box-rules@rule-set/geosite-ir.srs",
    ],
    "geoip-ir": [
        "https://raw.githubusercontent.com/Chocolate4U/Iran-sing-box-rules/rule-set/geoip-ir.srs",
        "https://cdn.jsdelivr.net/gh/chocolate4u/Iran-sing-box-rules@rule-set/geoip-ir.srs",
    ],
    "geosite-category-ads-all": [
        "https://raw.githubusercontent.com/Chocolate4U/Iran-sing-box-rules/rule-set/geosite-category-ads-all.srs",
        "https://cdn.jsdelivr.net/gh/chocolate4u/Iran-sing-box-rules@rule-set/geosite-category-ads-all.srs",
    ],
}

MIN_VALID_SIZE = 1024


def needed_rulesets(settings) -> list:
    names = ["geosite-ir", "geoip-ir"]
    if settings["routing"].get("block_ads"):
        names.append("geosite-category-ads-all")
    return names


def update_rulesets(settings) -> dict:
    os.makedirs(settings.rules_dir, exist_ok=True)
    report = {}
    client = client_for(settings)
    dns_server = settings["dns"]["local_server"]
    # Same TUN-bypass as subfetch.py: this box own traffic must never
    # depend on the tunnel it is itself building (see net.py docstring).
    with net.bypass_tun(dns_server):
        for name in needed_rulesets(settings):
            dest = os.path.join(settings.rules_dir, f"{name}.srs")
            ok = False
            for url in _RULESET_MIRRORS[name]:
                try:
                    resp = client.get(url, timeout=60, headers={"User-Agent": "curl/8.5.0"})
                    if resp.status_code == 200 and len(resp.content) > MIN_VALID_SIZE:
                        tmp = dest + ".tmp"
                        with open(tmp, "wb") as fh:
                            fh.write(resp.content)
                        os.replace(tmp, dest)
                        log.info("rule-set %s updated (%d bytes) <- %s",
                                 name, len(resp.content), url)
                        ok = True
                        break
                except Exception as exc:
                    log.debug("mirror %s for %s failed: %s", url, name, exc)
            report[name] = ok
            if not ok and not os.path.exists(dest):
                log.error("failed to download rule-set %s!", name)
    return report


def existing_rulesets(settings) -> list:
    out = []
    for name in _RULESET_MIRRORS:
        path = os.path.join(settings.rules_dir, f"{name}.srs")
        if os.path.isfile(path) and os.path.getsize(path) > MIN_VALID_SIZE:
            out.append(name)
    return out
