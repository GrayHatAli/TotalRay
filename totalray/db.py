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
        """Backwards-compatible alias for older scheduler code."""
        return self.get_pool_configs(pool, max_n)

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
