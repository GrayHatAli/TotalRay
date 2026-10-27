"""Load and merge config.yaml with the built-in defaults for TotalRay."""
from __future__ import annotations

import copy
import os

import yaml

DEFAULTS: dict = {
    "subscriptions": [],
    "schedule": {
        "sub_update_minutes": 360,
        "pool_a_test_minutes": 15,
        "pool_b_test_minutes": 3,
        "rules_update_hours": 24,
        "pool_a_max_items_per_round": 0,
        "pool_a_retry_backoff_minutes": 0,
        "traffic_sample_seconds": 60,
    },
    "retention": {
        # Soft-deleted configs (removed=1) are never tested or revived again;
        # they only serve as a dedupe marker for INSERT OR IGNORE on
        # `fingerprint`. Compact first (drop the heavy columns), delete later.
        "enabled": True,
        "prune_every_hours": 24,
        "vacuum_every_days": 7,
        "removed_compact_days": 3,
        "removed_delete_days": 45,
        # Configs that passed at least once stay restorable for longer.
        "removed_ever_ok_compact_days": 30,
        "removed_ever_ok_delete_days": 90,
        "log_days": 30,
        "traffic_log_days": 90,
        "batch_size": 5000,
    },
    "test": {
        "url": "https://www.gstatic.com/generate_204",
        "timeout_seconds": 10,
        "concurrency": 16,
        "retries": 1,
        "fail_threshold": -5,
        "ping_threshold_ms": 3000,
        "max_in_group": 50,
        "base_port": 24000,
        "chunk_size": 96,
    },
    "proxy_group": {
        "urltest_interval": "3m",
        "urltest_tolerance": 50,
        "idle_timeout": "30m",
        # Number of live pool-B outbounds kept in the selector. The first is
        # active and the remaining entries are warm standbys.
        "standby_count": 3,
    },
    "singbox_core": {
        "enabled": True,
        "socket": "/run/totalray/singbox-core.sock",
        "binary": "/usr/local/libexec/totalray/singbox-core",
    },
    "live_monitor": {
        # Live connection monitoring for real-time failover on packet drops.
        # Enabled by default to handle streaming/video call stability issues.
        "enabled": True,
        "check_interval_seconds": 2.0,  # How often to check connection health
        "error_threshold": 3,  # Errors in window before triggering failover
        "cooldown_seconds": 60.0,  # Minimum time between failovers
        "hysteresis_samples": 3,
        "error_window_seconds": 10.0,
        "retransmit_threshold": 3,
        "retransmit_sample_seconds": 5.0,
        "probe_interval_seconds": 7.0,
        "probe_timeout_seconds": 5.0,
        "probe_stale_seconds": 20.0,
        "circuit_breaker_failures": 3,
        "circuit_breaker_window_seconds": 300.0,
        "shadow_mode": True,
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
        "port": 2080,
    },
    "lan_proxy": {
        # A second SOCKS5+HTTP inbound (sing-box "mixed" type), bound to
        # 0.0.0.0 (not just 127.0.0.1 like local_proxy) so devices
        # elsewhere on the LAN can point straight at the Pi's own IP and
        # a fixed port instead of relying on the DHCP default-gateway
        # transparent-redirect setup. Kept as a *separate* inbound/port
        # from local_proxy so nothing that already relies on
        # 127.0.0.1:local_proxy.port (e.g. the git-over-proxy SSH config)
        # is affected. Username/password are required since this listens
        # on the LAN, not just localhost.
        "enabled": True,
        "listen": "0.0.0.0",
        "port": 2081,
        "username": "totalray",
        "password": "CHANGE_ME",
    },
    "apply": {
        # ApplyCoordinator settings -- controls restart throttling and
        # circuit breaker for sing-box restarts.
        "restart_cooldown_seconds": 60,
        "max_restarts_per_window": 5,
        "restart_window_seconds": 600,
        "health_check_retries": 3,
        "health_check_delay_seconds": 2.0,
    },
    "paths": {
        "data_dir": "/var/lib/totalray",
        "rules_dir": "/etc/sing-box/rules",
        "sing_box_config": "/etc/sing-box/config.json",
        "sing_box_bin": "/usr/bin/sing-box",
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

    @property
    def subscriptions(self) -> list:
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
        return os.path.join(self.data_dir, "totalray.db")

    @property
    def rules_dir(self) -> str:
        return self.data["paths"]["rules_dir"]

    def ensure_dirs(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.rules_dir, exist_ok=True)
