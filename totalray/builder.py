"""Build the final sing-box config (transparent gateway mode) for TotalRay.

This module is a near-exact port of the original builder with references
updated for TotalRay naming and comments.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess

from .rulesets import existing_rulesets
from . import net

log = logging.getLogger(__name__)

DNS_IN_PORT = 1053
REDIRECT_INPUT_MARK = net.REDIRECT_INPUT_MARK
REDIRECT_OUTPUT_MARK = net.REDIRECT_OUTPUT_MARK
_ALLOWED_MATCHERS = {
    "domain", "domain_suffix", "domain_keyword", "domain_regex",
    "ip_cidr", "source_ip_cidr", "port", "port_range", "network",
    "protocol", "rule_set", "ip_is_private", "inbound",
}


def _custom_rule(entry: dict) -> dict | None:
    match = {k: v for k, v in (entry.get("match") or {}).items()
             if k in _ALLOWED_MATCHERS}
    if not match:
        return None
    action = entry.get("action", "route")
    rule = dict(match)
    if action == "reject":
        rule["action"] = "reject"
    else:
        outbound = entry.get("outbound", "direct")
        if outbound not in ("direct", "select"):
            outbound = "direct"
        rule["action"] = "route"
        rule["outbound"] = outbound
    return rule


def build_config(settings, group: list) -> dict:
    routing = settings["routing"]
    tun = settings["tun"]
    dns_cfg = settings["dns"]
    pg = settings["proxy_group"]
    clash = settings["clash_api"]
    have_rules = set(existing_rulesets(settings))

    cfg_tags = []
    outbounds = []
    for item in group:
        tag = f"cfg-{item['id']}"
        ob = dict(item["outbound"])
        ob["tag"] = tag
        outbounds.append(ob)
        cfg_tags.append(tag)

    if not cfg_tags:
        log.warning("verified pool (b) is empty; everything routes direct for now")
        cfg_tags = ["direct"]

    group_outbounds = [
        {"type": "selector", "tag": "select",
         "outbounds": ["auto", "direct"] + cfg_tags,
         "default": "auto",
         "interrupt_exist_connections": True},
        {"type": "urltest", "tag": "auto",
         "outbounds": cfg_tags,
         "url": settings["test"]["url"],
         "interval": pg["urltest_interval"],
         "tolerance": int(pg["urltest_tolerance"]),
         "idle_timeout": pg["idle_timeout"],
         "interrupt_exist_connections": True},
    ]
    outbounds = group_outbounds + outbounds + [
        {"type": "direct", "tag": "direct"},
    ]

    tun_in = {
        "type": "tun", "tag": "tun-in",
        "interface_name": tun["interface"],
        "address": ["172.19.0.1/30"],
        "mtu": int(tun["mtu"]),
        "auto_route": True,
        "auto_redirect": True,
        "strict_route": True,
        "stack": tun["stack"],
        "auto_redirect_input_mark": REDIRECT_INPUT_MARK,
        "auto_redirect_output_mark": REDIRECT_OUTPUT_MARK,
    }
    if tun["stack"] != "system":
        tun_in["endpoint_independent_nat"] = True
    inbounds = [
        tun_in,
        {"type": "direct", "tag": "dns-in",
         "listen": "127.0.0.1", "listen_port": DNS_IN_PORT},
        {"type": "mixed", "tag": "local-proxy-in",
         "listen": "127.0.0.1", "listen_port": int(settings["local_proxy"]["port"])},
    ]

    rules = [
        {"action": "sniff"},
        {"inbound": "dns-in", "action": "hijack-dns"},
        {"protocol": "dns", "action": "hijack-dns"},
        {"ip_is_private": True, "action": "route", "outbound": "direct"},
    ]
    if routing.get("block_quic"):
        rules.append({"network": "udp", "port": 443, "action": "reject"})
    if routing.get("block_ads") and "geosite-category-ads-all" in have_rules:
        rules.append({"rule_set": ["geosite-category-ads-all"],
                      "action": "reject"})
    for entry in routing.get("custom_rules") or []:
        rule = _custom_rule(entry)
        if rule:
            rules.append(rule)
    if routing.get("iran_direct"):
        rules.append({"domain_suffix": [".ir"],
                      "action": "route", "outbound": "direct"})
        if "geosite-ir" in have_rules:
            rules.append({"rule_set": ["geosite-ir"], "action": "route", "outbound": "direct"})
        if "geoip-ir" in have_rules:
            rules.append({"rule_set": ["geoip-ir"], "action": "route", "outbound": "direct"})

    rule_sets = [
        {"tag": name, "type": "local", "format": "binary",
         "path": os.path.join(settings.rules_dir, f"{name}.srs")}
        for name in sorted(have_rules)
    ]

    route = {
        "rules": rules,
        "rule_set": rule_sets,
        "final": "select",
        "auto_detect_interface": True,
        "default_domain_resolver": "local",
    }

    dns_rules = []
    if routing.get("block_ads") and "geosite-category-ads-all" in have_rules:
        dns_rules.append({"rule_set": ["geosite-category-ads-all"],
                          "action": "reject"})
    if routing.get("iran_direct"):
        dns_rules.append({"domain_suffix": [".ir"], "server": "local"})
        if "geosite-ir" in have_rules:
            dns_rules.append({"rule_set": ["geosite-ir"], "server": "local"})
    dns = {
        "servers": [
            {"type": "https", "tag": "remote",
             "server": dns_cfg["remote_server"], "detour": "select"},
            {"type": "udp", "tag": "local",
             "server": dns_cfg["local_server"]},
        ],
        "rules": dns_rules,
        "final": "remote",
    }
    if dns_cfg.get("prefer_ipv4"):
        dns["strategy"] = "prefer_ipv4"

    experimental = {
        "cache_file": {
            "enabled": True,
            "path": os.path.join(settings["paths"]["sing_box_data_dir"],
                                 "sing-cache.db"),
        },
        "clash_api": {"external_controller": clash["listen"]},
    }
    if clash.get("secret"):
        experimental["clash_api"]["secret"] = clash["secret"]

    return {
        "log": {"level": "warn", "timestamp": True},
        "dns": dns,
        "inbounds": inbounds,
        "outbounds": outbounds,
        "route": route,
        "experimental": experimental,
    }


def write_and_check(settings, config: dict) -> tuple[bool, str]:
    target = settings["paths"]["sing_box_config"]
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
    check = subprocess.run([settings["paths"]["sing_box_bin"], "check", "-c", tmp],
                           capture_output=True, text=True, timeout=120)
    if check.returncode != 0:
        msg = (check.stderr or check.stdout or "unknown")[-800:]
        log.error("sing-box check failed: %s", msg)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False, msg
    os.replace(tmp, target)
    return True, target


def restart_singbox() -> tuple[bool, str]:
    try:
        proc = subprocess.run(["systemctl", "restart", "sing-box"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, "restarted"
    return False, (proc.stderr or proc.stdout or "systemctl failed")[-300:]


def rebuild_and_apply(settings, db) -> tuple[bool, str]:
    max_n = int(settings["test"]["max_in_group"])
    group = db.get_pool_configs("b", max_n)
    config = build_config(settings, group)
    ok, msg = write_and_check(settings, config)
    if not ok:
        return False, msg
    ok, msg = restart_singbox()
    if not ok:
        return False, f"config written but restart failed: {msg}"
    log.info("new config applied (%d configs in group)", len(group))
    return True, f"{len(group)} configs in group"
