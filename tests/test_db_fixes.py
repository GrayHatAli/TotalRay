"""Database regressions: retry backoff snapshot and test-round finish time."""
from __future__ import annotations

import re

import pytest

from totalray.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    sub = database.add_subscription("http://example.invalid/sub")
    database.sync_configs(sub, [
        {"fingerprint": f"f{i}", "name": f"n{i}", "link": "",
         "outbound": {"type": "direct"}} for i in range(3)])
    return database


def _ids(snapshot):
    return {c["id"] for c in snapshot["configs"]}


def test_retry_backoff_skips_recently_tested_configs(db):
    everything = _ids(db.get_pool_snapshot("a"))
    assert len(everything) == 3
    recent, old, never = sorted(everything)
    with db._lock, db._conn:
        db._conn.execute(
            "UPDATE configs SET last_test_at=datetime('now') WHERE id=?", (recent,))
        db._conn.execute(
            "UPDATE configs SET last_test_at=datetime('now','-30 minutes') WHERE id=?", (old,))

    # no backoff: everything is a candidate
    assert _ids(db.get_pool_snapshot("a")) == everything
    # 5 minute backoff: the one tested just now is skipped
    assert _ids(db.get_pool_snapshot("a", retry_backoff_minutes=5)) == {old, never}
    # never-tested configs are always candidates, oldest first
    ordered = [c["id"] for c in db.get_pool_snapshot("a", retry_backoff_minutes=5)["configs"]]
    assert ordered[0] == never


def test_retry_backoff_respects_max_n(db):
    snap = db.get_pool_snapshot("a", max_n=2, retry_backoff_minutes=5)
    assert len(snap["configs"]) == 2


def test_auto_finished_round_gets_a_real_timestamp(db):
    cid = sorted(_ids(db.get_pool_snapshot("a")))[0]
    result = db.record_pool_a_results({cid: 100}, 3000, -5)
    row = db._conn.execute(
        "SELECT state, finished_at FROM test_rounds WHERE id=?",
        (result["round_id"],)).fetchone()
    assert row["state"] == "finished"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", row["finished_at"])


def test_running_round_has_no_finish_time(db):
    cid = sorted(_ids(db.get_pool_snapshot("a")))[0]
    snap = db.get_pool_snapshot("a")
    rid = db.start_test_round("a", snap["generation"], total=1)
    db.record_pool_a_results({cid: 100}, 3000, -5, round_id=rid,
                             snapshot_generation=snap["generation"])
    row = db._conn.execute(
        "SELECT state, finished_at FROM test_rounds WHERE id=?", (rid,)).fetchone()
    assert row["state"] == "running"
    assert row["finished_at"] is None


def test_database_with_leftover_device_tables_still_opens(tmp_path):
    """Existing installs keep the old (now unused) device tables; that is harmless."""
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE devices (ip TEXT PRIMARY KEY, last_seen TEXT,"
        " last_rx INTEGER, last_tx INTEGER);"
        "CREATE TABLE device_traffic_log (id INTEGER PRIMARY KEY, ip TEXT, ts TEXT);"
        "INSERT INTO devices VALUES ('10.0.0.2', datetime('now'), 1, 2);")
    old.commit()
    old.close()
    database = Database(path)
    assert database.stats()["total"] == 0
    database.prune({})   # retention must not trip over them either
