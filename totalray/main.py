"""CLI manager - run:  totalray <command>  (or python -m totalray)"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import json
import time

import requests
import subprocess
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import __version__

TEHRAN_TZ = ZoneInfo("Asia/Tehran")


def _setup_logging(settings, console_output: bool = True) -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if console_output:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)
    try:
        fileh = logging.handlers.RotatingFileHandler(
            os.path.join(settings.data_dir, "totalray.log"),
            maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fileh.setFormatter(fmt)
        root.addHandler(fileh)
    except OSError:
        pass


def _load(args):
    from .db import Database
    from .settings import Settings
    settings = Settings(args.config)
    settings.ensure_dirs()
    return settings, Database(settings.db_path)


# --------------------------------------------------------------- commands

def cmd_init(args):
    settings, db = _load(args)
    print(f"database ready: {settings.db_path}")
    print(f"rule-set path: {settings.rules_dir}")
    db.close()


def cmd_add_sub(args):
    settings, db = _load(args)
    sub_id = db.add_subscription(args.url)
    print(f"subscription registered (id={sub_id})")
    print("  note: add the URL to config.yaml too if you want it to persist.")
    if args.now:
        from . import subfetch
        print(subfetch.update_all(settings, db))
    db.close()


def cmd_del_sub(args):
    settings, db = _load(args)
    db.remove_subscription(args.id)
    print(f"subscription #{args.id} removed")
    db.close()


def cmd_list(args):
    settings, db = _load(args)
    for sub in db.list_subscriptions():
        state = "enabled" if sub["enabled"] else "disabled"
        print(f"  #{sub['id']:<3} [{state}] {sub['url']}")
        print(f"        last status: {sub['last_status'] or '-'} |"
              f" count: {sub['last_count']} | updated: {sub['last_update'] or '-'}")
    stats = db.stats()
    print(f"\nconfigs: {stats['total']} total |"
          f" pool A (candidates): {stats['pool_a']} |"
          f" pool B (verified): {stats['pool_b']} |"
          f" removed: {stats['removed']}")
    db.close()


def cmd_update_subs(args):
    settings, db = _load(args)
    from . import subfetch
    summary = subfetch.update_all(settings, db)
    print(f"done: {summary}")
    db.close()


def cmd_update_rules(args):
    settings, db = _load(args)
    from . import rulesets
    report = rulesets.update_rulesets(settings)
    for name, ok in report.items():
        print(f"  {'OK' if ok else 'FAILED'}  {name}")
    db.close()


def cmd_test_a(args):
    settings, db = _load(args)
    from .scheduler import Manager
    stats = Manager(settings, db).run_pool_a_round()
    print(f"pool-A round: {stats}")
    db.close()


def cmd_test_b(args):
    settings, db = _load(args)
    from .scheduler import Manager
    stats = Manager(settings, db).run_pool_b_round()
    print(f"pool-B round: {stats}")
    db.close()


def cmd_build(args):
    settings, db = _load(args)
    from .coordinator import ApplyCoordinator
    coordinator = ApplyCoordinator(settings, db)
    ok, msg = coordinator.apply()
    print(("OK: " if ok else "FAILED: ") + msg)
    db.close()
    sys.exit(0 if ok else 1)


def cmd_run(args):
    settings, db = _load(args)
    from .scheduler import run_daemon
    run_daemon(settings, db)
    db.close()


def _title_bar(label: str, width: int = 70) -> str:
    text = f" {label} "
    pad = max(0, width - len(text))
    left = pad // 2
    right = pad - left
    return ("-" * left) + text + ("-" * right)


def _truncate(value, n: int) -> str:
    value = str(value)
    return value if len(value) <= n else value[: max(0, n - 3)] + "..."


def _fmt_table(headers, rows) -> str:
    """Render a simple bordered/pipe table, widths sized to content, with a
    separator line after the header and after every data row."""
    widths = [len(str(h)) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(str(cell)))

    def _row(cells) -> str:
        return "| " + " | ".join(str(c).center(w) for c, w in zip(cells, widths)) + " |"

    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    lines = [_row(headers), sep]
    for r in rows:
        lines.append(_row(r))
        lines.append(sep)
    return "\n".join(lines)


def _parse_db_ts(ts_str):
    """Parse SQLite's UTC datetime('now') string and convert to Tehran."""
    if not ts_str:
        return None
    try:
        dt = datetime.strptime(str(ts_str)[:19], "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc).astimezone(TEHRAN_TZ)
    except (ValueError, TypeError):
        return None


def _fmt_hhmm(ts_str) -> str:
    dt = _parse_db_ts(ts_str)
    return dt.strftime("%H:%M") if dt else "-"


def _fmt_datetime(ts_str) -> str:
    dt = _parse_db_ts(ts_str)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "-"


def _next_round(ts_str, minutes: int) -> str:
    dt = _parse_db_ts(ts_str)
    if not dt:
        return "-"
    return (dt + timedelta(minutes=minutes)).strftime("%H:%M")


def _fmt_ago(epoch_ts) -> str:
    if not epoch_ts:
        return "-"
    try:
        secs = time.time() - float(epoch_ts)
    except (TypeError, ValueError):
        return "-"
    secs = max(0, secs)
    if secs < 60:
        return f"{int(secs)}s ago"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    return f"{int(secs // 3600)}h ago"


def _read_live_monitor_status(settings) -> dict | None:
    path = os.path.join(settings.data_dir, "live_monitor_status.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _read_round_status(settings) -> dict:
    path = os.path.join(settings.data_dir, "round_status.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _read_apply_state(settings) -> dict | None:
    path = os.path.join(settings.data_dir, "apply_state.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _status_snapshot(settings, db) -> dict:
    """Collect every status section into a single dict for --json output."""
    stats = db.stats()
    sched = settings["schedule"]
    round_status = _read_round_status(settings)
    apply_state = _read_apply_state(settings)
    lm = _read_live_monitor_status(settings)

    # -- connection --------------------------------------------------------
    status_line = _live_connection_status(settings, db)
    totals = _get_traffic_totals(settings)

    # -- apply coordinator -------------------------------------------------
    apply = {"available": apply_state is not None}
    if apply_state:
        apply.update({
            "circuit_open": apply_state.get("circuit_open", False),
            "restarts_total": apply_state.get("restart_count_total", 0),
            "restart_failures_total": apply_state.get("restart_failures_total", 0),
            "last_restart_at": apply_state.get("last_restart_at"),
            "last_restart_reason": apply_state.get("last_restart_reason"),
            "last_restart_ok": apply_state.get("last_restart_ok", False),
        })

    # -- subscriptions -----------------------------------------------------
    subs = db.list_subscriptions()
    sub_minutes = int(sched.get("sub_update_minutes", 30))
    health_by_sub = db.configs_health_by_sub()
    sub_list = []
    for sub in subs:
        rs = round_status.get("subscriptions") or {}
        sub_list.append({
            "id": sub["id"],
            "url": sub["url"],
            "enabled": bool(sub.get("enabled", 1)),
            "last_count": sub.get("last_count"),
            "healthy": health_by_sub.get(sub["id"], 0),
            "last_update": sub.get("last_update"),
            "state": rs.get("state", "idle"),
            "round_id": rs.get("round_id"),
        })

    # -- pool A ------------------------------------------------------------
    pa = round_status.get("pool_a") or {}
    pool_a = {
        "count": stats["pool_a"],
        "last_round": stats["last_test_a"],
        "state": pa.get("state", "idle"),
        "round_id": pa.get("round_id"),
        "snapshot_generation": pa.get("snapshot_generation"),
        "items_total": pa.get("items_total"),
        "items_processed": pa.get("items_processed"),
        "items_ok": pa.get("items_ok"),
        "items_failed": pa.get("items_failed"),
        "last_error": pa.get("last_error"),
    }

    # -- pool B ------------------------------------------------------------
    pb = round_status.get("pool_b") or {}
    pool_b = {
        "count": stats["pool_b"],
        "last_round": stats["last_test_b"],
        "state": pb.get("state", "idle"),
        "round_id": pb.get("round_id"),
        "snapshot_generation": pb.get("snapshot_generation"),
        "items_total": pb.get("items_total"),
        "items_processed": pb.get("items_processed"),
        "items_ok": pb.get("items_ok"),
        "items_failed": pb.get("items_failed"),
        "last_error": pb.get("last_error"),
    }

    # -- devices -----------------------------------------------------------
    devices = []
    try:
        devs = db.get_device_totals()
        if not devs:
            from .traffic import get_device_stats
            devs = get_device_stats(settings)
        if devs:
            for d in devs:
                devices.append({
                    "ip": d.get("ip"),
                    "last_seen": d.get("last_seen"),
                    "rx": d.get("last_rx", d.get("download", 0)),
                    "tx": d.get("last_tx", d.get("upload", 0)),
                })
    except Exception:  # noqa: BLE001
        pass

    return {
        "status_line": status_line,
        "traffic": {"down": totals[0], "up": totals[1]} if totals else None,
        "live_monitor": lm,
        "apply": apply,
        "subscriptions": sub_list,
        "configs": {
            "total": stats["total"],
            "pool_a": stats["pool_a"],
            "pool_b": stats["pool_b"],
            "removed": stats["removed"],
        },
        "pool_a": pool_a,
        "pool_b": pool_b,
        "devices": devices,
    }


def _round_wait_reason(current: dict) -> str:
    """Render the human reason for a queued/skipped round, e.g.
    'internet down (blocked by internet)' or just 'busy'."""
    parts = []
    reason = current.get("reason")
    if reason:
        parts.append(str(reason).replace("_", " "))
    blocked = current.get("blocked_by")
    if blocked:
        parts.append(f"blocked by {blocked}")
    return f" ({'; '.join(parts)})" if parts else ""


def _round_display(status: dict, kind: str, fallback_last: str,
                   fallback_next: str) -> tuple[str, str]:
    """Render live round state for one job kind.

    Prefers the concurrency-safe state written by the daemon's
    RoundStateStore; falls back to the last test_log timestamps when a
    job is idle or no state file exists yet. Distinct states per the
    architecture plan: running, queued, skipped, failed.
    """
    current = status.get(kind) or {}
    state = current.get("state")
    if state == "running" or current.get("running"):
        started = current.get("started_at")
        elapsed = _fmt_ago(started) if started else "now"
        text = f"running ({elapsed})"
        processed = current.get("items_processed")
        total = current.get("items_total")
        if processed is not None and total:
            text += f" {processed:,}/{total:,}"
        if current.get("round_id"):
            text += f" round={current['round_id']}"
        return text, "in progress"
    if state == "queued":
        return f"queued{_round_wait_reason(current)}", "pending"
    if state == "skipped":
        return f"skipped{_round_wait_reason(current)}", "-"
    if state == "failed":
        err = current.get("last_error")
        suffix = f": {_truncate(err, 40)}" if err else ""
        return f"failed{suffix}", "-"
    return fallback_last, fallback_next


def _get_traffic_totals(settings):
    """Cumulative download/upload since sing-box started, from the Clash
    API's /connections endpoint (downloadTotal/uploadTotal fields)."""
    from .http_client import client_for

    client = client_for(settings)
    clash = settings["clash_api"]
    host, _, port = clash["listen"].partition(":")
    host = host or "127.0.0.1"
    headers = {}
    if clash.get("secret"):
        headers["Authorization"] = f"Bearer {clash['secret']}"
    try:
        resp = client.get(f"http://{host}:{port}/connections", headers=headers, timeout=3)
        resp.raise_for_status()
        data = resp.json()
        return data.get("downloadTotal", 0), data.get("uploadTotal", 0)
    except Exception:  # noqa: BLE001
        return None


def _human_bytes(n) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return str(n)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n} {unit}"
        n = n // 1024
    return f"{n} PB"


# api.ipify.org alone is a single point of failure: a lot of "what's my
# IP" services get resolution or connect trouble, and this box's DNS path
# in particular has a history of flaking on individual providers. Try a
# short list of independent, free, plain-text IP-echo services in order
# and use whichever answers first, instead of failing the whole status
# check because one specific provider is having a bad day.
_EXIT_IP_PROVIDERS = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
    "https://checkip.amazonaws.com",
    "https://ipinfo.io/ip",
]


def _fetch_exit_ip(timeout: int = 5) -> tuple[str | None, str | None]:
    """Try each provider in _EXIT_IP_PROVIDERS in turn; return
    (ip, None) on the first success, or (None, last_error) if all fail.
    """
    last_err = "no providers configured"
    for url in _EXIT_IP_PROVIDERS:
        try:
            probe = subprocess.run(
                ["curl", "-4", "-fsS", "--max-time", str(timeout), url],
                capture_output=True, text=True, timeout=timeout + 2, check=True)
            ip = probe.stdout.strip()
            # Cheap sanity check -- a provider returning an HTML error page
            # or similar should not be treated as a valid IP.
            if ip and ip.replace(".", "").isdigit() and ip.count(".") == 3:
                return ip, None
            last_err = f"{url} returned unexpected output: {ip[:50]!r}"
        except Exception as exc:  # noqa: BLE001
            last_err = f"{url}: {type(exc).__name__}"
    return None, last_err


def _live_connection_status(settings, db) -> str:
    """Ask sing-box's Clash API which server the 'auto' group actually has
    selected right now, then independently check the apparent public exit
    IP (ipify) and see if it matches that server. This is a real
    connectivity check, not just "is the service running": a config can be
    selected in sing-box's routing table while itself being dead, and
    ipify alone can't tell you *which* server you're behind.
    """
    from .http_client import client_for

    client = client_for(settings)
    clash = settings["clash_api"]
    host, _, port = clash["listen"].partition(":")
    host = host or "127.0.0.1"
    headers = {}
    if clash.get("secret"):
        headers["Authorization"] = f"Bearer {clash['secret']}"

    # Status is a bounded snapshot, not a retried data-fetch operation. A
    # dead upstream must not make `totalray status` hang for minutes.
    status_session = requests.Session()
    status_session.trust_env = False
    try:
        resp = status_session.get(
            f"http://{host}:{port}/proxies/auto",
            headers=headers, timeout=(2, 3))
        resp.raise_for_status()
        now = resp.json().get("now", "")
    except Exception as exc:  # noqa: BLE001
        return f"status        : disconnected (sing-box / clash API unreachable: {exc})"

    if now == "direct" or not now:
        return "status        : disconnected (no verified server selected - pool B is empty)"

    row = None
    if now.startswith("cfg-"):
        with db._lock:  # noqa: SLF001 - read-only single-row lookup
            r = db._conn.execute(
                "SELECT * FROM configs WHERE id=?", (now[4:],)).fetchone()
            row = dict(r) if r else None

    server = None
    name = now
    if row:
        name = row.get("name") or now
        try:
            server = json.loads(row["outbound"]).get("server")
        except Exception:  # noqa: BLE001
            server = None

    exit_ip, exit_err = _fetch_exit_ip()
    if exit_ip is None:
        return (f"status        : disconnected (selected {name}; exit IP check"
                f" failed on all providers: {exit_err} - DNS/proxy may be unavailable)")

    server_ip = server
    if server and not server.replace(".", "").isdigit():
        try:
            resolved = subprocess.run(
                ["getent", "ahostsv4", server],
                capture_output=True, text=True, timeout=3, check=True)
            server_ip = resolved.stdout.split()[0] if resolved.stdout else None
        except (OSError, subprocess.SubprocessError):
            server_ip = None

    if server_ip and exit_ip == server_ip:
        return f"status        : connected (via {name}, exit IP {exit_ip} matches)"
    if server_ip:
        return (f"status        : degraded (selected {name}, but exit IP {exit_ip}"
                f" != server IP {server_ip} - traffic may be routing elsewhere)")
    return (f"status        : connected? (selected {name}, exit IP {exit_ip},"
            f" could not resolve server address to confirm)")


def cmd_status(args):
    settings, db = _load(args)

    # -- JSON output mode ---------------------------------------------------
    if getattr(args, "json", False):
        data = _status_snapshot(settings, db)
        print(json.dumps(data, default=str, indent=2))
        db.close()
        return

    stats = db.stats()
    last_a = stats["last_test_a"]
    last_b = stats["last_test_b"]
    round_status = _read_round_status(settings)
    sched = settings["schedule"]

    # -- overview --------------------------------------------------------
    print(_title_bar("overview"))
    status_line = _live_connection_status(settings, db)
    totals = _get_traffic_totals(settings)
    if totals:
        status_line += f"  (Down: {_human_bytes(totals[0])} / Up: {_human_bytes(totals[1])})"
    print(status_line)

    lm = _read_live_monitor_status(settings)
    if lm:
        state = "active" if lm.get("running") else "stopped"
        heartbeat_gap = time.time() - lm["last_check"] if lm.get("last_check") else None
        stale = ""
        if lm.get("running") and heartbeat_gap is not None and heartbeat_gap > max(30, lm.get("check_interval", 2) * 10):
            stale = " (stale - no recent heartbeat, check the service)"
        print(f"live monitor  : {state}{stale} |"
              f" {lm.get('failover_count', 0)} failovers |"
              f" last failover: {_fmt_ago(lm.get('last_failover'))}")
    else:
        print("live monitor  : status unknown (no heartbeat file yet - restart totalray.service)")

    # -- apply coordinator --------------------------------------------------
    apply_state = _read_apply_state(settings)
    if apply_state:
        last_at = apply_state.get("last_restart_at")
        reason = apply_state.get("last_restart_reason", "-")
        ok = apply_state.get("last_restart_ok", False)
        circuit = apply_state.get("circuit_open", False)
        restarts = apply_state.get("restart_count_total", 0)
        failures = apply_state.get("restart_failures_total", 0)
        last_display = _fmt_ago(last_at) if last_at else "-"
        circuit_display = "OPEN (restarts paused)" if circuit else "closed"
        print(f"apply         : {circuit_display} | {restarts} restarts |"
              f" {failures} failures | last={last_display}")
        if last_at and reason:
            print(f"  last reason : {reason.replace('_', ' ')}"
                  f" ({'ok' if ok else 'failed'})")
    else:
        print("apply         : no restart history yet")

    # -- subscriptions ----------------------------------------------------
    print()
    print(_title_bar("Subscriptions"))
    subs = db.list_subscriptions()
    sub_minutes = int(sched.get("sub_update_minutes", 30))
    health_by_sub = db.configs_health_by_sub()
    sub_rows = []
    for i, sub in enumerate(subs, 1):
        count = sub.get("last_count")
        count = count if count is not None else "-"
        healthy = health_by_sub.get(sub["id"], 0)
        if sub.get("last_status") and sub["last_status"] != "ok":
            last_round = "failed"
        else:
            last_round = _fmt_hhmm(sub.get("last_update"))
        next_round = _next_round(sub.get("last_update"), sub_minutes) if sub.get("enabled", 1) else "-"
        last_round, next_round = _round_display(
            round_status, "subscriptions", last_round, next_round)
        sub_rows.append([i, _truncate(sub["url"], 38), count, healthy, last_round, next_round])
    if sub_rows:
        print(_fmt_table(["No", "Url", "Configs", "Healthy", "Last Round", "Next Round"], sub_rows))
        print("  Configs = how many the last fetch parsed; Healthy = how many of those")
        print("  are still alive right now (pool A or B, not removed). Configs=0 means")
        print("  the fetch itself found nothing; Configs>0 but Healthy=0 means they were")
        print("  parsed fine but every one failed real-connectivity testing.")
    else:
        print("  (no subscriptions configured)")

    print()
    print(f"configs : {stats['total']} total |"
          f" pool A (candidates): {stats['pool_a']} |"
          f" pool B (verified): {stats['pool_b']} |"
          f" removed: {stats['removed']}")

    # -- Pool A -------------------------------------------------------------
    print()
    print(_title_bar("Pool A"))
    pool_a_minutes = int(sched.get("pool_a_test_minutes", 15))
    pa = round_status.get("pool_a") or {}
    # Show live round state even when the database has no finished round yet
    # (e.g. the first-ever round is still running).
    if last_a or pa.get("state"):
        last_a_ts = last_a["ts"] if last_a else None
        last_display, next_display = _round_display(
            round_status, "pool_a", _fmt_hhmm(last_a_ts),
            _next_round(last_a_ts, pool_a_minutes))
        rows_a = [[last_a["total"] if last_a else "-",
                   last_a["ok"] if last_a else "-",
                   last_a["removed"] if last_a else "-",
                   last_display, next_display]]
        print(_fmt_table(["Total", "Promoted", "Removed", "Last Round", "Next Round"], rows_a))
        gen = pa.get("snapshot_generation")
        rid = pa.get("round_id")
        if gen is not None or rid:
            parts = []
            if gen is not None:
                parts.append(f"generation={gen}")
            if rid:
                parts.append(f"round={rid}")
            print(f"  {', '.join(parts)}")
    else:
        print("  (no pool-A test rounds yet)")
    worst = db.worst_configs(5)
    if worst:
        print("\n-- close to removal --")
        for row in worst:
            print(f"  score {row['score']:>3}  [pool {row['pool']}]  {row['name']}")

    # -- Pool B -------------------------------------------------------------
    print()
    print(_title_bar("Pool B"))
    pool_b_minutes = int(sched.get("pool_b_test_minutes", 3))
    pb = round_status.get("pool_b") or {}
    if last_b or pb.get("state"):
        last_b_ts = last_b["ts"] if last_b else None
        last_display, next_display = _round_display(
            round_status, "pool_b", _fmt_hhmm(last_b_ts),
            _next_round(last_b_ts, pool_b_minutes))
        rows_b = [[last_b["total"] if last_b else "-",
                   last_b["ok"] if last_b else "-",
                   last_b["failed"] if last_b else "-",
                   last_display, next_display]]
        print(_fmt_table(["Total", "Healthy", "Demoted", "Last Round", "Next Round"], rows_b))
        gen = pb.get("snapshot_generation")
        rid = pb.get("round_id")
        if gen is not None or rid:
            parts = []
            if gen is not None:
                parts.append(f"generation={gen}")
            if rid:
                parts.append(f"round={rid}")
            print(f"  {', '.join(parts)}")
    else:
        print("  (no pool-B test rounds yet)")
    top = db.top_configs(5)
    if top:
        print("\n-- top 5 verified configs (lowest real latency) --")
        for i, row in enumerate(top, 1):
            print(f"  {i:>2}. {row['last_delay']:>5} ms  {row['name']}")

    # -- connected devices (per-device traffic sampler) ---------------------
    try:
        devs = db.get_device_totals()
        if not devs:
            from .traffic import get_device_stats
            devs = get_device_stats(settings)
        if devs:
            print("\n-- connected devices (syncbox) --")
            for d in devs:
                ip = d.get('ip')
                last_seen = _fmt_datetime(d.get('last_seen'))
                rx = d.get('last_rx', d.get('download', 0))
                tx = d.get('last_tx', d.get('upload', 0))
                try:
                    rows2 = db.get_recent_device_log(ip, limit=1)
                    if rows2:
                        r = rows2[0]
                        rxd = int(r.get('rx_delta', 0) or 0)
                        txd = int(r.get('tx_delta', 0) or 0)
                        sample_ts = _fmt_datetime(r.get('ts'))
                    else:
                        rxd = txd = 0
                        sample_ts = '-'
                except Exception:
                    rxd = txd = 0
                    sample_ts = '-'
                print(f"  {ip}  last_seen={last_seen}  down={_human_bytes(rx)} (+{_human_bytes(rxd)})"
                      f"  up={_human_bytes(tx)} (+{_human_bytes(txd)})  last_sample={sample_ts}")
    except Exception:
        pass

    db.close()


def cmd_failed_requests(args):
    settings, db = _load(args)
    log_path = os.path.join(settings.data_dir, "totalray_failed_requests.log")
    if not os.path.exists(log_path):
        print("no failed-requests log found at", log_path)
        db.close()
        return
    try:
        with open(log_path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except Exception as exc:
        print("error reading log:", exc)
        db.close()
        return
    n = int(getattr(args, 'lines', 20) or 20)
    tail = lines[-n:] if n > 0 else lines
    if getattr(args, 'json', False):
        for l in tail:
            print(l)
        db.close()
        return
    for l in tail:
        try:
            obj = json.loads(l)
        except Exception:
            print(l)
            continue
        ts = obj.get("ts", "-")
        method = obj.get("method", "-")
        url = obj.get("url", "-")
        status = obj.get("status_code") if "status_code" in obj else obj.get("error", "-")
        snippet = obj.get("response_snippet", "") or obj.get("kwargs", {}).get("data", "")
        snippet = (snippet.replace("\n", " ")[:200]) if isinstance(snippet, str) else ""
        print(f"{ts} {method} {status} {url}")
        if snippet:
            print(f"  {snippet}")
    db.close()
    # follow mode: stream appended lines
    if getattr(args, 'follow', False):
        try:
            with open(log_path, "r", encoding="utf-8") as fh:
                # move to end
                fh.seek(0, os.SEEK_END)
                import time as _time
                while True:
                    line = fh.readline()
                    if not line:
                        _time.sleep(0.5)
                        continue
                    line = line.rstrip("\n")
                    if getattr(args, 'json', False):
                        print(line)
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        print(line)
                        continue
                    ts = obj.get("ts", "-")
                    method = obj.get("method", "-")
                    url = obj.get("url", "-")
                    status = obj.get("status_code") if "status_code" in obj else obj.get("error", "-")
                    snippet = obj.get("response_snippet", "") or obj.get("kwargs", {}).get("data", "")
                    snippet = (snippet.replace("\n", " ")[:200]) if isinstance(snippet, str) else ""
                    print(f"{ts} {method} {status} {url}")
                    if snippet:
                        print(f"  {snippet}")
        except KeyboardInterrupt:
            return


# --------------------------------------------------------------- argparse

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="totalray",
        description="sing-box client manager for a Raspberry Pi transparent gateway")
    parser.add_argument("--config", default="/opt/totalray/config.yaml",
                        help="path to config.yaml")
    parser.add_argument("--version", action="version",
                        version=f"totalray {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and directories").set_defaults(fn=cmd_init)

    p = sub.add_parser("add-sub", help="add a subscription")
    p.add_argument("url")
    p.add_argument("--now", action="store_true",
                   help="fetch it immediately too")
    p.set_defaults(fn=cmd_add_sub)

    p = sub.add_parser("del-sub", help="remove a subscription")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_del_sub)

    sub.add_parser("list", help="show subscriptions and stats").set_defaults(fn=cmd_list)
    sub.add_parser("update-subs", help="manually refresh subscriptions").set_defaults(fn=cmd_update_subs)
    sub.add_parser("update-rules", help="refresh the Iran rule-sets").set_defaults(fn=cmd_update_rules)
    sub.add_parser("test-a", help="run one pool-A (candidate) test round").set_defaults(fn=cmd_test_a)
    sub.add_parser("test-b", help="run one pool-B (verified) test round").set_defaults(fn=cmd_test_b)
    sub.add_parser("build", help="build and apply the sing-box config").set_defaults(fn=cmd_build)
    sub.add_parser("run", help="run the scheduler daemon (service)").set_defaults(fn=cmd_run)
    p = sub.add_parser("status", help="status and ranking of configs")
    p.add_argument("--json", action="store_true",
                   help="print machine-readable JSON output")
    p.set_defaults(fn=cmd_status)
    p = sub.add_parser("failed-requests", help="show recent failed HTTP requests")
    p.add_argument("-n", "--lines", type=int, default=20, help="number of recent entries to show")
    p.add_argument("--json", action="store_true", help="print raw JSON lines")
    p.add_argument("-f", "--follow", action="store_true", help="follow the log (like tail -f)")
    p.set_defaults(fn=cmd_failed_requests)

    args = parser.parse_args(argv)
    settings, _ = _load_minimal(args)
    # Keep status output clean; diagnostics remain in rotating logs.
    _setup_logging(settings, console_output=args.command != "status")
    args.fn(args)


def _load_minimal(args):
    """Settings only - the database is opened inside each command."""
    from .settings import Settings
    settings = Settings(args.config)
    settings.ensure_dirs()
    return settings, None


if __name__ == "__main__":
    main()
