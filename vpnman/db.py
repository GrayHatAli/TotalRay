"""SQLite storage layer: subscriptions, configs, dual-pool state, scoring.

Dual-pool model
----------------
Every config lives in exactly one of two pools:

  pool 'a' (candidate pool) - everything fetched from subscriptions.
                               Tested every `pool_a_test_minutes`.
  pool 'b' (verified pool)  - configs that passed a real connectivity
                               test with latency <= ping_threshold_ms.
                               Tested every `pool_b_test_minutes`.
                               The load balancer / sing-box outbound
                               group is built ONLY from this pool, so
                               the active tunnel only ever uses
                               already-proven-good servers.

Movement rules
--------------
  - Pool A round: passing configs move a -> b (score reset to 0).
                  Failing configs (unreachable OR latency above the
                  threshold) stay in pool a and lose 1 point of score.
  - Pool B round: passing configs stay in b (score reset to 0).
                  Failing configs move b -> a and lose 1 point of score.
  - Whenever score reaches `fail_threshold` (default -5), the config is
    soft-removed (removed=1) regardless of which pool/round caused it.

Soft-delete means: if a subscription serves the exact same config again
later, its failure history is preserved instead of starting from zero.
"""
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
        """Add columns introduced after the initial release, if missing.

        Safe to run on a fresh database too (columns already exist there
        via SCHEMA, so ALTER TABLE is simply skipped).
        """
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(configs)")}
        if "pool" not in cols:
            self._conn.execute(
                "ALTER TABLE configs ADD COLUMN pool TEXT NOT NULL DEFAULT 'a'")
            # Anything that was already considered "healthy" under the old
            # single-list model is a reasonable candidate to start in the
            # verified pool, so it isn't dropped from the active group.
            self._conn.execute(
                "UPDATE configs SET pool='b' WHERE removed=0 AND last_delay>0")
        tlog_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(test_log)")}
        if "pool" not in tlog_cols:
            self._conn.execute(
                "ALTER TABLE test_log ADD COLUMN pool TEXT NOT NULL DEFAULT 'a'")
        # Safe to (re)create now that the "pool" column is guaranteed to exist.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_configs_pool ON configs(removed, pool)")

    def close(self):
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------- subscriptions
    def add_subscription(self, url: str) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO subscriptions(url) VALUES (?)", (url,))
            if cur.lastrowid:
                return cur.lastrowid
            row = self._conn.execute(
                "SELECT id FROM subscriptions WHERE url=?", (url,)).fetchone()
            return row["id"]

    def remove_subscription(self, sub_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))

    def list_subscriptions(self) -> list:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM subscriptions ORDER BY id")]

    def enabled_subscriptions(self) -> list:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM subscriptions WHERE enabled=1 ORDER BY id")]

    def set_sub_status(self, sub_id: int, status: str, count: int = 0) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE subscriptions SET last_update=datetime('now'),"
                " last_status=?, last_count=? WHERE id=?",
                (status, count, sub_id))

    # ------------------------------------------------------- configs
    def sync_configs(self, sub_id: int, items: list) -> int:
        """Insert new configs; duplicates (even removed ones) are left untouched.

        New configs always start in pool 'a' (schema default) so they go
        through a real test before ever being eligible for the active
        (pool 'b') outbound group.
        """
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

    def get_pool_candidates(self, pool: str) -> list:
        """All non-removed configs currently in the given pool ('a' or 'b')."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, outbound FROM configs"
                " WHERE removed=0 AND pool=? ORDER BY id", (pool,)).fetchall()
        return [{"id": r["id"], "name": r["name"],
                 "outbound": json.loads(r["outbound"])} for r in rows]

    def _apply_score(self, cid: int, delay, ping_threshold: int,
                      fail_threshold: int, promote_to: str, demote_to: str) -> str:
        """Update score/pool/last_delay for one config based on a test result.

        Returns one of: 'ok' (passed, now/still in promote_to pool),
        'failed' (missed threshold, moved to demote_to pool),
        'removed' (score hit fail_threshold, soft-deleted).
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
        self._conn.execute(
            "UPDATE configs SET score=?, last_delay=?, pool=?,"
            " last_test_at=datetime('now') WHERE id=?",
            (score, stored_delay, demote_to, cid))
        return "failed"

    def record_pool_a_results(self, results: dict, ping_threshold: int,
                               fail_threshold: int = -5) -> dict:
        """results: {config_id: delay_ms or -1}. Passing configs -> pool b."""
        return self._record_round("a", results, ping_threshold, fail_threshold,
                                   promote_to="b", demote_to="a")

    def record_pool_b_results(self, results: dict, ping_threshold: int,
                               fail_threshold: int = -5) -> dict:
        """results: {config_id: delay_ms or -1}. Failing configs -> pool a."""
        return self._record_round("b", results, ping_threshold, fail_threshold,
                                   promote_to="b", demote_to="a")

    def _record_round(self, pool: str, results: dict, ping_threshold: int,
                       fail_threshold: int, promote_to: str, demote_to: str) -> dict:
        ok = failed = 0
        removed_ids = []
        with self._lock, self._conn:
            for cid, delay in results.items():
                outcome = self._apply_score(cid, delay, ping_threshold,
                                            fail_threshold, promote_to, demote_to)
                if outcome == "ok":
                    ok += 1
                elif outcome == "failed":
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
        """Configs in a pool, ordered by lowest latency first (nulls last)."""
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

    # ------------------------------------------------------- reporting
    def stats(self) -> dict:
        with self._lock:
            def one(q, *p):
                return self._conn.execute(q, p).fetchone()[0]
            return {
                "total": one("SELECT COUNT(*) FROM configs"),
                "pool_a": one("SELECT COUNT(*) FROM configs WHERE removed=0 AND pool='a'"),
                "pool_b": one("SELECT COUNT(*) FROM configs WHERE removed=0 AND pool='b'"),
                "removed": one("SELECT COUNT(*) FROM configs WHERE removed=1"),
                "subs": one("SELECT COUNT(*) FROM subscriptions"),
                "last_test_a": (self._conn.execute(
                    "SELECT * FROM test_log WHERE pool='a' ORDER BY id DESC LIMIT 1")
                    .fetchone()),
                "last_test_b": (self._conn.execute(
                    "SELECT * FROM test_log WHERE pool='b' ORDER BY id DESC LIMIT 1")
                    .fetchone()),
            }

    def top_configs(self, limit: int = 10) -> list:
        """Best-performing configs currently in the verified pool (b)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, last_delay, score FROM configs"
                " WHERE removed=0 AND pool='b' AND last_delay>0"
                " ORDER BY last_delay ASC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def worst_configs(self, limit: int = 10) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, pool, score FROM configs"
                " WHERE removed=0 AND score<0"
                " ORDER BY score ASC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def unremove(self, fingerprint: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE configs SET removed=0, pool='a', score=0, last_delay=NULL"
                " WHERE fingerprint=?", (fingerprint,))
            return cur.rowcount > 0
