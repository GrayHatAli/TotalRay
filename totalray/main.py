"""CLI manager - run:  totalray <command>  (or python -m totalray)"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import json
import time
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
    from . import builder
    ok, msg = builder.rebuild_and_apply(settings, db)
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


def _round_display(status: dict, kind: str, fallback_last: str,
                   fallback_next: str) -> tuple[str, str]:
    current = status.get(kind) or {}
    if current.get("running"):
        started = current.get("started_at")
        elapsed = _fmt_ago(started) if started else "now"
        return f"running ({elapsed})", "in progress"
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


def _live_connection_status(settings, db) -> str:
    """Ask sing-box's Clash API which server the 'auto' group actually has
    selected right now, then independently check the apparent public exit
    IP (ipify) and see if it matches that server. This is a real
    connectivity check, not just "is the service running": a config can be
    selected in sing-box's routing table while itself being dead, and
    ipify alone can't tell you *which* server you're behind.
    """
    import socket
    from .http_client import client_for

    client = client_for(settings)
    clash = settings["clash_api"]
    host, _, port = clash["listen"].partition(":")
    host = host or "127.0.0.1"
    headers = {}
    if clash.get("secret"):
        headers["Authorization"] = f"Bearer {clash['secret']}"

    try:
        resp = client.get(f"http://{host}:{port}/proxies/auto",
                          headers=headers, timeout=5)
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

    try:
        exit_ip = client.get("https://api.ipify.org", timeout=8).text.strip()
    except Exception as exc:  # noqa: BLE001
        return (f"status        : disconnected (selected {name}, but no exit IP"
                f" response: {exc})")

    server_ip = server
    if server and not server.replace(".", "").isdigit():
        try:
            server_ip = socket.gethostbyname(server)
        except OSError:
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
    if last_a:
        last_display, next_display = _round_display(
            round_status, "pool_a", _fmt_hhmm(last_a["ts"]),
            _next_round(last_a["ts"], pool_a_minutes))
        rows_a = [[last_a["total"], last_a["ok"], last_a["removed"],
                   last_display, next_display]]
        print(_fmt_table(["Total", "Promoted", "Removed", "Last Round", "Next Round"], rows_a))
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
    if last_b:
        last_display, next_display = _round_display(
            round_status, "pool_b", _fmt_hhmm(last_b["ts"]),
            _next_round(last_b["ts"], pool_b_minutes))
        rows_b = [[last_b["total"], last_b["ok"], last_b["failed"],
                   last_display, next_display]]
        print(_fmt_table(["Total", "Healthy", "Demoted", "Last Round", "Next Round"], rows_b))
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
    sub.add_parser("status", help="status and ranking of configs").set_defaults(fn=cmd_status)
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
