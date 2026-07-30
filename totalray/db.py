"""SQLite storage layer for TotalRay with device traffic persistence."""
from __future__ import annotations

import json
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT UNIQUE NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_update TEXT,
    last_status TEXT,
    last_count  INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS configs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT UNIQUE NOT NULL,
    name         TEXT,
    link         TEXT,
    outbound     TEXT NOT NULL,
    source_sub   INTEGER,
    pool         TEXT NOT NULL DEFAULT 'a',   -- 'a' candidate | 'b' verified
    score        INTEGER NOT NULL DEFAULT 0,
    last_delay   INTEGER,              -- NULL=untested | -1=unreachable | >0 latency ms
    removed      INTEGER NOT NULL DEFAULT 0,
    last_ok_at   TEXT,
    last_test_at TEXT,
    created_at   TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_configs_active ON configs(removed, last_delay);
CREATE TABLE IF NOT EXISTS test_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT DEFAULT (datetime('now')),
    pool    TEXT NOT NULL DEFAULT 'a',
    total   INTEGER,
    ok      INTEGER,
    failed  INTEGER,
    removed INTEGER
);

-- Devices known to TotalRay (tracked by IP)
CREATE TABLE IF NOT EXISTS devices (
    ip       TEXT PRIMARY KEY,
    last_seen TEXT,
    last_rx  INTEGER NOT NULL DEFAULT 0,
    last_tx  INTEGER NOT NULL DEFAULT 0
);

-- Device traffic log: per-sample deltas and cumulative totals
CREATE TABLE IF NOT EXISTS device_traffic_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ip      TEXT NOT NULL,
    ts      TEXT DEFAULT (datetime('now')),
    rx_delta INTEGER NOT NULL DEFAULT 0,
    tx_delta INTEGER NOT NULL DEFAULT 0,
    rx_total INTEGER NOT NULL DEFAULT 0,
    tx_total INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_device_traffic_ip_ts ON device_traffic_log(ip, ts);
"""


class Database:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        # future migrations can be placed here; ensure indexes exist
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_configs_pool ON configs(removed, pool)")

    def close(self):
        with self._lock:
            self._conn.close()

    # ---------------- subscriptions / configs (essential methods)
    def add_subscription(self, url: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO subscriptions(url) VALUES (?)", (url,))
            if cur.lastrowid:
                return cur.lastrowid
            row = self._conn.execute(
                "SELECT id FROM subscriptions WHERE url=?", (url,)).fetchone()
            return row["id"]

    def list_subscriptions(self) -> list:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM subscriptions ORDER BY id")]

    def enabled_subscriptions(self) -> list:
        """Backwards-compatible alias for older subfetch.py code."""
        return self.list_subscriptions()

    def set_sub_status(self, sub_id: int, status: str, count: int = None) -> None:
        """Record the outcome of the last fetch attempt for a subscription."""
        with self._lock, self._conn:
            if count is not None:
                self._conn.execute(
                    "UPDATE subscriptions SET last_status=?, last_count=?,"
                    " last_update=datetime('now') WHERE id=?",
                    (status, count, sub_id))
            else:
                self._conn.execute(
                    "UPDATE subscriptions SET last_status=?,"
                    " last_update=datetime('now') WHERE id=?",
                    (status, sub_id))

    def sync_configs(self, sub_id: int, items: list) -> int:
        added = 0
        with self._lock, self._conn:
            for item in items:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO configs"
                    " (fingerprint, name, link, outbound, source_sub)"
                    " VALUES (?,?,?,?,?)",
                    (item["fingerprint"], item["name"], item["link"],
                     json.dumps(item["outbound"], ensure_ascii=False), sub_id))
                added += cur.rowcount
        return added

    def _apply_score(self, cid: int, delay, ping_threshold: int,
                      fail_threshold: int, promote_to: str, demote_to: str,
                      demote_threshold: int = 0) -> str:
        """Update score/pool/last_delay for one config based on a test result.

        `demote_threshold` lets a caller require more than one consecutive
        failure before the config's *pool* actually changes (score still
        decrements every time either way). This matters specifically for
        pool-B: every pool membership change makes rebuild_and_apply()
        restart sing-box, and sing-box has a known bug where frequent
        restarts leave stale kernel routes and start crash-looping
        (https://github.com/SagerNet/sing-box/issues/3572). A single
        marginal ping crossing the threshold and bouncing back next round
        used to demote-then-immediately-repromote the same config twice
        in a row, causing two restarts for nothing; requiring e.g. 2
        consecutive failures absorbs that without meaningfully delaying
        the removal of a genuinely dead config.

        Returns one of: 'ok' (passed, now/still in promote_to pool),
        'grace' (failed, but under demote_threshold - score docked, pool
        unchanged), 'failed' (failed past demote_threshold, moved to
        demote_to pool), 'removed' (score hit fail_threshold, soft-deleted).
        Caller must hold the connection/lock already.
        """
        row = self._conn.execute(
            "SELECT score FROM configs WHERE id=?", (cid,)).fetchone()
        if row is None:
            return "skip"
        passed = delay is not None and delay > 0 and delay <= ping_threshold
        if passed:
            self._conn.execute(
                "UPDATE configs SET score=0, last_delay=?, pool=?,"
                " last_ok_at=datetime('now'), last_test_at=datetime('now')"
                " WHERE id=?",
                (int(delay), promote_to, cid))
            return "ok"
        score = row["score"] - 1
        stored_delay = int(delay) if (delay is not None and delay > 0) else -1
        if score <= fail_threshold:
            self._conn.execute(
                "UPDATE configs SET score=?, last_delay=?, removed=1,"
                " last_test_at=datetime('now') WHERE id=?",
                (score, stored_delay, cid))
            return "removed"
        if score <= demote_threshold:
            self._conn.execute(
                "UPDATE configs SET score=?, last_delay=?, pool=?,"
                " last_test_at=datetime('now') WHERE id=?",
                (score, stored_delay, demote_to, cid))
            return "failed"
        self._conn.execute(
            "UPDATE configs SET score=?, last_delay=?,"
            " last_test_at=datetime('now') WHERE id=?",
            (score, stored_delay, cid))
        return "grace"

    def record_pool_a_results(self, results: dict, ping_threshold: int,
                               fail_threshold: int = -5) -> dict:
        """results: {config_id: delay_ms or -1}. Passing configs -> pool b."""
        return self._record_round("a", results, ping_threshold, fail_threshold,
                                   promote_to="b", demote_to="a")

    def record_pool_b_results(self, results: dict, ping_threshold: int,
                               fail_threshold: int = -5,
                               demote_grace: int = 2) -> dict:
        """results: {config_id: delay_ms or -1}. Failing configs -> pool a,
        but only after `demote_grace` consecutive failures."""
        return self._record_round("b", results, ping_threshold, fail_threshold,
                                   promote_to="b", demote_to="a",
                                   demote_threshold=-demote_grace)

    def _record_round(self, pool: str, results: dict, ping_threshold: int,
                       fail_threshold: int, promote_to: str, demote_to: str,
                       demote_threshold: int = 0) -> dict:
        ok = failed = 0
        removed_ids = []
        with self._lock, self._conn:
            for cid, delay in results.items():
                outcome = self._apply_score(cid, delay, ping_threshold,
                                            fail_threshold, promote_to, demote_to,
                                            demote_threshold)
                if outcome == "ok":
                    ok += 1
                elif outcome in ("failed", "grace"):
                    failed += 1
                elif outcome == "removed":
                    failed += 1
                    removed_ids.append(cid)
            self._conn.execute(
                "INSERT INTO test_log(pool, total, ok, failed, removed)"
                " VALUES (?,?,?,?,?)",
                (pool, len(results), ok, failed, len(removed_ids)))
        return {"total": len(results), "ok": ok,
                "failed": failed, "removed": removed_ids}

    def get_pool_configs(self, pool: str, max_n: int = 0) -> list:
        with self._lock:
            tested = self._conn.execute(
                "SELECT id, name, outbound, last_delay FROM configs"
                " WHERE removed=0 AND pool=? AND last_delay>0"
                " ORDER BY last_delay ASC", (pool,)).fetchall()
            untested = self._conn.execute(
                "SELECT id, name, outbound, last_delay FROM configs"
                " WHERE removed=0 AND pool=? AND last_delay IS NULL"
                " ORDER BY id", (pool,)).fetchall()
        rows = list(tested) + list(untested)
        if max_n and max_n > 0:
            rows = rows[:max_n]
        return [{"id": r["id"], "name": r["name"],
                 "outbound": json.loads(r["outbound"]),
                 "delay": r["last_delay"]} for r in rows]

    def get_pool_candidates(self, pool: str, max_n: int = 0) -> list:
        """All non-removed configs in this pool, for the purpose of
        picking who gets (re)tested this round.

        This is deliberately NOT the same query as get_pool_configs():
        that one only returns entries with a valid positive last_delay
        (right for building the live sing-box group from known-good pool
        B members), which silently excludes anything whose last test was
        a hard failure (last_delay == -1, not NULL and not > 0). Reusing
        it here meant a config that ever timed out fell into a gap it
        could never leave - never retested, never promoted, never scored
        down to removal. Candidate selection must include every
        non-removed row in the pool, full stop.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, outbound, last_delay FROM configs"
                " WHERE removed=0 AND pool=?"
                " ORDER BY last_test_at IS NOT NULL, last_test_at",
                (pool,)).fetchall()
        if max_n and max_n > 0:
            rows = rows[:max_n]
        return [{"id": r["id"], "name": r["name"],
                 "outbound": json.loads(r["outbound"]),
                 "delay": r["last_delay"]} for r in rows]

    def top_configs(self, limit: int = 10) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, last_delay FROM configs"
                " WHERE removed=0 AND pool='b' AND last_delay>0"
                " ORDER BY last_delay ASC LIMIT ?", (limit,)).fetchall()
        return [{"name": r["name"], "last_delay": r["last_delay"]} for r in rows]

    def worst_configs(self, limit: int = 5) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, pool, score FROM configs"
                " WHERE removed=0"
                " ORDER BY score ASC LIMIT ?", (limit,)).fetchall()
        return [{"name": r["name"], "pool": r["pool"], "score": r["score"]} for r in rows]

    # ---------------- device traffic persistence
    def record_device_stats(self, devices: list) -> None:
        """Persist per-device cumulative counters and insert per-sample deltas.

        `devices` is a list of dicts: {ip, upload, download} where numbers are
        cumulative byte counters as read from nft/iptables (or best-effort).
        """
        with self._lock, self._conn:
            for d in devices:
                ip = d.get("ip")
                upload = int(d.get("upload", 0))
                download = int(d.get("download", 0))
                row = self._conn.execute(
                    "SELECT last_rx, last_tx FROM devices WHERE ip=?", (ip,)).fetchone()
                if row is None:
                    # new device
                    self._conn.execute(
                        "INSERT INTO devices(ip, last_seen, last_rx, last_tx)"
                        " VALUES (?, datetime('now'), ?, ?)",
                        (ip, download, upload))
                    rx_delta = download
                    tx_delta = upload
                else:
                    prev_rx = int(row["last_rx"] or 0)
                    prev_tx = int(row["last_tx"] or 0)
                    rx_delta = max(0, download - prev_rx)
                    tx_delta = max(0, upload - prev_tx)
                    self._conn.execute(
                        "UPDATE devices SET last_seen=datetime('now'), last_rx=?, last_tx=? WHERE ip=?",
                        (download, upload, ip))
                if rx_delta or tx_delta:
                    self._conn.execute(
                        "INSERT INTO device_traffic_log(ip, rx_delta, tx_delta, rx_total, tx_total)"
                        " VALUES (?,?,?,?,?)",
                        (ip, rx_delta, tx_delta, download, upload))

    def get_device_totals(self) -> list:
        """Return per-device current totals and last-seen timestamp.

        Returns list of dicts: {ip, last_seen, last_rx, last_tx} ordered by last_seen desc.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT ip, last_seen, last_rx, last_tx FROM devices ORDER BY last_seen DESC").fetchall()
        return [dict(r) for r in rows]

    def get_recent_device_log(self, ip: str, limit: int = 50) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, rx_delta, tx_delta, rx_total, tx_total FROM device_traffic_log"
                " WHERE ip=? ORDER BY ts DESC LIMIT ?", (ip, limit)).fetchall()
        return [dict(r) for r in rows]

    # ---------------- reporting helpers
    def stats(self) -> dict:
        with self._lock:
            def one(q, *p):
                return self._conn.execute(q, p).fetchone()[0]
            # last test rounds (if any)
            def last_round(pool: str):
                row = self._conn.execute(
                    "SELECT ts, total, ok, failed, removed FROM test_log WHERE pool=? ORDER BY ts DESC LIMIT 1",
                    (pool,)).fetchone()
                return dict(row) if row else None

            return {
                "total": one("SELECT COUNT(*) FROM configs"),
                "pool_a": one("SELECT COUNT(*) FROM configs WHERE removed=0 AND pool='a'"),
                "pool_b": one("SELECT COUNT(*) FROM configs WHERE removed=0 AND pool='b'"),
                "removed": one("SELECT COUNT(*) FROM configs WHERE removed=1"),
                "subs": one("SELECT COUNT(*) FROM subscriptions"),
                "last_test_a": last_round('a'),
                "last_test_b": last_round('b'),
            }
