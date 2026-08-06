"""Build the final sing-box config (transparent gateway mode) for TotalRay.

This module is a near-exact port of the original builder with references
updated for TotalRay naming and comments.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time

import requests

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

    # NOTE: "auto" used to be a sing-box `urltest` group, which let sing-box
    # itself run periodic latency probes and silently switch outbounds on its
    # own -- fighting with TotalRay's own pool-B scoring/hysteresis logic and
    # forcing a full config rebuild + sing-box restart (dropping every active
    # connection) whenever TotalRay wanted to react to a score change.
    # "auto" is now a plain `selector`: TotalRay is the only thing that ever
    # changes which member is active, via the Clash API (see
    # set_active_config()), without touching TUN/routing or restarting
    # sing-box. interrupt_exist_connections is False on both groups so that
    # switching the active member does not kill in-flight connections
    # (video calls, downloads, etc.) -- only new connections take the new
    # route.
    group_outbounds = [
        {"type": "selector", "tag": "select",
         "outbounds": ["auto", "direct"] + cfg_tags,
         "default": "auto",
         "interrupt_exist_connections": False},
        {"type": "selector", "tag": "auto",
         "outbounds": cfg_tags,
         "default": cfg_tags[0],
         "interrupt_exist_connections": False},
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
    # Optional second SOCKS5+HTTP inbound, bound to the LAN (not just
    # localhost like local-proxy-in above), so individual devices can point
    # straight at the Pi's own IP + this port and use the tunnel directly --
    # without the router's DHCP default-gateway redirect dance. Kept as a
    # separate inbound/port from local-proxy-in so nothing that already
    # relies on 127.0.0.1:local_proxy.port (e.g. the git-over-proxy SSH
    # config) is affected by adding auth here. Always requires
    # username/password since, unlike local-proxy-in, this is reachable
    # from other devices on the network.
    lan_proxy = settings["lan_proxy"]
    if lan_proxy.get("enabled"):
        inbounds.append({
            "type": "mixed", "tag": "lan-proxy-in",
            "listen": lan_proxy.get("listen", "0.0.0.0"),
            "listen_port": int(lan_proxy["port"]),
            "users": [{"username": lan_proxy["username"],
                      "password": lan_proxy["password"]}],
        })

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


def _group_state_path(settings) -> str:
    return os.path.join(settings["paths"]["data_dir"], "last_group_tags.json")


def _load_last_tags(settings) -> set[str] | None:
    """Tag set that was active in sing-box as of the last full rebuild.
    None means unknown (e.g. first run) -- caller should treat that as
    "changed" and do a full rebuild to be safe."""
    path = _group_state_path(settings)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return set(json.load(fh))
    except (OSError, ValueError):
        return None


def _save_last_tags(settings, tags: set[str]) -> None:
    path = _group_state_path(settings)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(sorted(tags), fh)
    except OSError:
        log.warning("failed to persist group state to %s", path)


def set_active_config(settings, tag: str, retries: int = 5, delay: float = 1.0) -> tuple[bool, str]:
    """Point the "auto" selector at `tag` via the Clash API, live -- no
    config rebuild, no sing-box restart, no TUN/route churn, and (since
    interrupt_exist_connections is False on this group) no impact on
    connections that are already in flight."""
    clash = settings["clash_api"]
    host = clash["listen"]
    secret = clash.get("secret") or ""
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    url = f"http://{host}/proxies/auto"
    last_err = ""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.put(url, json={"name": tag}, headers=headers, timeout=5)
        except (requests.RequestException, OSError) as exc:
            last_err = str(exc)
        else:
            if resp.status_code in (200, 204):
                return True, "switched"
            last_err = f"clash api returned {resp.status_code}: {resp.text[:200]}"
        if attempt < retries:
            time.sleep(delay)
    return False, last_err


def restart_singbox() -> tuple[bool, str]:
    try:
        proc = subprocess.run(["systemctl", "restart", "sing-box"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, "restarted"
    return False, (proc.stderr or proc.stdout or "systemctl failed")[-300:]


def rebuild_and_apply(settings, db, force: bool = False) -> tuple[bool, str]:
    """Apply the current pool-B state to sing-box.

    Most rounds, the *set* of verified servers hasn't actually changed --
    only their relative ranking has. In that (common) case we skip the
    rebuild/restart entirely and just repoint the "auto" selector at the
    new best pick over the Clash API, which is instant and does not touch
    TUN, routing, or any in-flight connection.

    A full rebuild + sing-box restart only happens when the outbound tag
    set genuinely changed (a server was promoted/demoted/removed), or when
    `force=True` is passed (e.g. after a rule-set update, where the route
    section itself changed and a restart is unavoidable).
    """
    max_n = int(settings["test"]["max_in_group"])
    group = db.get_pool_configs("b", max_n)
    new_tags = {f"cfg-{item['id']}" for item in group}
    best_tag = f"cfg-{group[0]['id']}" if group else "direct"

    last_tags = _load_last_tags(settings)
    if not force and last_tags is not None and last_tags == new_tags:
        ok, msg = set_active_config(settings, best_tag)
        if ok:
            log.info("active config switched to %s via Clash API (no restart)", best_tag)
            return True, f"switched to {best_tag} (no restart)"
        log.warning("clash api switch failed (%s); falling back to full rebuild", msg)
        # fall through to the full rebuild below as a safety net

    config = build_config(settings, group)
    ok, msg = write_and_check(settings, config)
    if not ok:
        return False, msg
    ok, msg = restart_singbox()
    if not ok:
        return False, f"config written but restart failed: {msg}"
    _save_last_tags(settings, new_tags)
    ok2, msg2 = set_active_config(settings, best_tag)
    if not ok2:
        log.warning("post-restart clash api selection failed: %s (selector default will be used instead)", msg2)
    log.info("new config applied (%d configs in group)", len(group))
    return True, f"{len(group)} configs in group"
