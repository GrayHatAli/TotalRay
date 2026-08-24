from __future__ import annotations

from totalray.db import Database


def _add_config(db: Database, fingerprint: str = "fp") -> int:
    sub_id = db.add_subscription("https://example.test/sub")
    db.sync_configs(sub_id, [{
        "fingerprint": fingerprint,
        "name": fingerprint,
        "link": "link",
        "outbound": {"type": "direct"},
    }])
    return db.get_pool_candidates("a")[0]["id"]


def test_pool_generation_changes_on_import_and_stale_result_is_recorded(tmp_path):
    db = Database(str(tmp_path / "totalray.db"))
    cid = _add_config(db)
    generation = db.pool_generation("a")
    round_id = db.start_test_round("a", generation, total=1)

    # Membership changed after the snapshot: the old result must not apply.
    db.sync_configs(db.list_subscriptions()[0]["id"], [{
        "fingerprint": "new-fp",
        "name": "new",
        "link": "link",
        "outbound": {"type": "direct"},
    }])
    stats = db.record_pool_a_results({cid: 10}, 100, round_id=round_id,
                                     snapshot_generation=generation)

    assert stats["stale"] == 1
    assert stats["ok"] == 0
    row = db._conn.execute(
        "SELECT state, snapshot_generation, stale FROM test_rounds WHERE id=?",
        (round_id,)).fetchone()
    assert dict(row) == {
        "state": "finished", "snapshot_generation": generation, "stale": 1}
    db.close()


def test_recording_pool_round_without_explicit_start_creates_test_round(tmp_path):
    db = Database(str(tmp_path / "totalray.db"))
    cid = _add_config(db)
    stats = db.record_pool_a_results({cid: 10}, 100)
    row = db._conn.execute(
        "SELECT id, pool, snapshot_generation, state, total, ok, failed, stale"
        " FROM test_rounds WHERE id=?", (stats["round_id"],)).fetchone()
    assert row["pool"] == "a"
    assert row["snapshot_generation"] == stats["snapshot_generation"]
    assert row["state"] == "finished"
    assert row["total"] == 1
    assert row["ok"] == 1
    assert row["stale"] == 0
    db.close()
