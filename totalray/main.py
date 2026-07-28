"""CLI manager - run:  totalray <command>  (or python -m totalray)"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import json

from . import __version__


def _setup_logging(settings) -> None:
    os.makedirs(settings.data_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
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


def cmd_status(args):
    settings, db = _load(args)
    stats = db.stats()
    last_a = stats["last_test_a"]
    last_b = stats["last_test_b"]
    print("-- overview --------------------------")
    print(f"  subscriptions : {stats['subs']}")
    print(f"  configs       : {stats['total']} total |"
          f" pool A (candidates): {stats['pool_a']} |"
          f" pool B (verified): {stats['pool_b']} |"
          f" removed: {stats['removed']}")
    if last_a:
        print(f"  last pool-A round: {last_a['ts']} - {last_a['ok']} promoted,"
              f" {last_a['failed']} still failing, {last_a['removed']} removed")
    if last_b:
        print(f"  last pool-B round: {last_b['ts']} - {last_b['ok']} still healthy,"
              f" {last_b['failed']} demoted, {last_b['removed']} removed")
    top = db.top_configs(10)
    if top:
        print("\n-- top 10 verified configs (lowest real latency) --")
        for i, row in enumerate(top, 1):
            print(f"  {i:>2}. {row['last_delay']:>5} ms  {row['name']}")
    worst = db.worst_configs(5)
    if worst:
        print("\n-- close to removal --")
        for row in worst:
            print(f"  score {row['score']:>3}  [pool {row['pool']}]  {row['name']}")

    # per-device section: prefer persisted totals from the DB, fallback to live sampler
    def _human(n: int) -> str:
        try:
            n = int(n or 0)
        except Exception:
            return str(n)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if n < 1024:
                return f"{n}{unit}"
            n = n // 1024
        return f"{n}PB"

    try:
        devs = db.get_device_totals()
        if not devs:
            from .traffic import get_device_stats
            devs = get_device_stats(settings)
        if devs:
            print("\n-- connected devices (syncbox) --")
            for d in devs:
                ip = d.get('ip') or d.get('ip')
                last_seen = d.get('last_seen', '-')
                rx = d.get('last_rx', d.get('download', 0))
                tx = d.get('last_tx', d.get('upload', 0))
                # try to show last-sample delta and timestamp if available
                try:
                    rows = db.get_recent_device_log(ip, limit=1)
                    if rows:
                        r = rows[0]
                        rxd = int(r.get('rx_delta', 0) or 0)
                        txd = int(r.get('tx_delta', 0) or 0)
                        sample_ts = r.get('ts', '-')
                    else:
                        rxd = txd = 0
                        sample_ts = '-'
                except Exception:
                    rxd = txd = 0
                    sample_ts = '-'
                print(f"  {ip}  last_seen={last_seen}  down={_human(rx)} (+{_human(rxd)})  up={_human(tx)} (+{_human(txd)})  last_sample={sample_ts}")
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
    _setup_logging(settings)
    args.fn(args)


def _load_minimal(args):
    """Settings only - the database is opened inside each command."""
    from .settings import Settings
    settings = Settings(args.config)
    settings.ensure_dirs()
    return settings, None


if __name__ == "__main__":
    main()
