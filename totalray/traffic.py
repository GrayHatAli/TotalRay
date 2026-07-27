"""Simple traffic sampler that attempts to read per-IP counters from nftables.

This is a best-effort implementation: if nft counters named/structured for
TotalRay exist, they will be parsed and returned as a list of dicts with
`ip`, `upload` and `download` bytes. If nft isn't available or parsing
fails, an empty list is returned.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess

log = logging.getLogger(__name__)


_IP_BYTES_RE = re.compile(r"(\d+\.\d+\.\d+\.\d+).*bytes\s+(\d+)")


def _parse_nft_output(text: str) -> list:
    out = []
    # Look for lines containing an IP address and a bytes counter
    for line in text.splitlines():
        m = _IP_BYTES_RE.search(line)
        if m:
            ip = m.group(1)
            b = int(m.group(2))
            out.append({"ip": ip, "bytes": b})
    return out


def get_device_stats(settings) -> list:
    """Return a list of device dicts: {ip, upload, download}.

    Currently attempts to read nft counters; if unavailable, returns [].
    """
    if not shutil.which("nft"):
        return []
    try:
        # List all counters; the admin can set up nft counters tagged for
        # TotalRay to collect per-IP bytes which this parser will pick up.
        out = subprocess.check_output(
            ["nft", "list", "counters"], text=True,
            stderr=subprocess.DEVNULL)
        parsed = _parse_nft_output(out)
        # The parser returns generic 'bytes' counts; we present them as both
        # upload/download if no split is available.
        devices = []
        for p in parsed:
            devices.append({"ip": p["ip"], "upload": 0, "download": p["bytes"]})
        return devices
    except subprocess.CalledProcessError as exc:
        log.debug("nft counters read failed: %s", exc)
        return []
    except Exception as exc:  # fallback
        log.debug("unexpected error reading device counters: %s", exc)
        return []
