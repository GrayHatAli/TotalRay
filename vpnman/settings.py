"""Load and merge config.yaml with the built-in defaults."""
from __future__ import annotations

import copy
import os

import yaml

DEFAULTS: dict = {
    "subscriptions": [],
    "schedule": {
        "sub_update_minutes": 360,
        "pool_a_test_minutes": 15,   # candidate pool retest interval
        "pool_b_test_minutes": 3,    # verified/active pool retest interval
        "rules_update_hours": 24,
    },
    "test": {
        "url": "https://www.gstatic.com/generate_204",
        "timeout_seconds": 10,
        "concurrency": 16,
        "retries": 1,
        "fail_threshold": -5,
        "ping_threshold_ms": 3000,   # max latency to count as "healthy"
        "max_in_group": 50,
        "base_port": 24000,
        "chunk_size": 96,           # inbounds per temporary test instance
    },
    "proxy_group": {
        "urltest_interval": "3m",
        "urltest_tolerance": 50,
        "idle_timeout": "30m",
    },
    "routing": {
        "iran_direct": True,
        "block_ads": False,
        "block_quic": False,
        "custom_rules": [],
    },
    "dns": {
        "remote_server": "1.1.1.1",
        "local_server": "192.168.1.1",
        "prefer_ipv4": True,
    },
    "tun": {
        "interface": "singtun0",
        "stack": "mixed",
        "mtu": 1500,
    },
    "clash_api": {
        "listen": "127.0.0.1:9090",
        "secret": "",
    },
    "local_proxy": {
        # Explicit SOCKS/HTTP proxy on the box itself, bound to the same
        # "select" group as the transparent TUN. Lets vpnman's own
        # processes (subscription fetching) deliberately opt into the
        # tunnel as a fallback, instead of relying on the all-or-nothing
        # transparent capture.
        "port": 2080,
    },
    "paths": {
        "data_dir": "/var/lib/vpnman",
        "rules_dir": "/etc/sing-box/rules",
        "sing_box_config": "/etc/sing-box/config.json",
        "sing_box_bin": "/usr/bin/sing-box",
        # Own state directory of the sing-box service (matches its -D
        # flag / package default). The cache-file MUST live here rather
        # than under vpnman's data_dir: the official sing-box systemd
        # unit is sandboxed and only has write access to its own state
        # directory, not to /var/lib/vpnman.
        "sing_box_data_dir": "/var/lib/sing-box",
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class Settings:
    def __init__(self, path: str):
        self.path = path
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        self.data = _merge(DEFAULTS, raw)

    def __getitem__(self, section: str) -> dict:
        return self.data[section]

    # --- convenience accessors ---
    @property
    def subscriptions(self) -> list:
        """Normalize to a list of {name, url, headers} dicts.

        `headers` lets a specific subscription override the User-Agent /
        add extra headers it requires - some panels (e.g. custom
        subscription.php scripts bound to a specific app's fingerprint)
        reject anything that doesn't look exactly like their own app,
        regardless of IP or TLS. Example in config.yaml:

            subscriptions:
              - url: "https://example.com/sub"
                headers:
                  User-Agent: "Happ/5.1.0/macos catalyst/2607150020650"
                  X-HWID: "..."
        """
        out = []
        for item in self.data.get("subscriptions") or []:
            if isinstance(item, str):
                out.append({"name": item[:48], "url": item, "headers": None})
            elif isinstance(item, dict) and item.get("url"):
                out.append({"name": item.get("name") or item["url"][:48],
                            "url": item["url"],
                            "headers": item.get("headers") or None})
        return out

    @property
    def data_dir(self) -> str:
        return self.data["paths"]["data_dir"]

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "vpnman.db")

    @property
    def rules_dir(self) -> str:
        return self.data["paths"]["rules_dir"]

    def ensure_dirs(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.rules_dir, exist_ok=True)
