"""Database retention: compact/delete removed configs, trim logs, VACUUM."""
from __future__ import annotations

import pytest

from totalray.db import Database

CFG = {
    "removed_compact_days": 3,
    "removed_delete_days": 45,
    "removed_ever_ok_compact_days": 30,
    "removed_ever_ok_delete_days": 90,
    "log_days": 30,
    "traffic_log_days": 90,
    "batch_size": 5000,
}
HEAVY = "x" * 2000


@pytest.fixture()
def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    yield database
    try:
        database.close()
    except Exception:  # noqa: BLE001
        pass


def _add(db, fp, *, removed, days, ever_ok=False, heavy=True):
    """Insert a config whose last test happened `days` ago."""
    with db._lock, db._conn:
        db._conn.execute(
            "INSERT INTO configs (fingerprint, name, link, outbound, removed,"
            " last_ok_at, last_test_at, created_at)"
            " VALUES (?,?,?,?,?, CASE WHEN ? THEN datetime('now', ?) END,"
            " datetime('now', ?), datetime('now', ?))",
            (fp, "n-" + fp, HEAVY if heavy else None, '{"type":"vless"}',
             int(removed), int(ever_ok), f"-{days + 1} days",
             f"-{days} days", f"-{days + 2} days"))


def _row(db, fp):
    return db._conn.execute(
        "SELECT name, link, outbound, removed FROM configs WHERE fingerprint=?",
        (fp,)).fetchone()


def _seed(db):
    _add(db, "live-old", removed=False, days=400)
    _add(db, "never-1d", removed=True, days=1)
    _add(db, "never-5d", removed=True, days=5)
    _add(db, "never-60d", removed=True, days=60)
    _add(db, "ever-10d", removed=True, days=10, ever_ok=True)
    _add(db, "ever-40d", removed=True, days=40, ever_ok=True)
    _add(db, "ever-120d", removed=True, days=120, ever_ok=True)


def test_prune_compacts_then_deletes_removed_configs(db):
    _seed(db)

    result = db.prune(CFG)

    assert result["configs_deleted_never_ok"] == 1
    assert result["configs_deleted_ever_ok"] == 1
    assert result["configs_compacted_never_ok"] == 1
    assert result["configs_compacted_ever_ok"] == 1
    # untouched: live rows and recently removed rows keep their full payload
    for fp in ("live-old", "never-1d", "ever-10d"):
        row = _row(db, fp)
        assert row["outbound"] == '{"type":"vless"}' and row["link"] == HEAVY
    # compacted: heavy columns gone, row (and its fingerprint) kept
    for fp in ("never-5d", "ever-40d"):
        row = _row(db, fp)
        assert row["outbound"] == "" and row["link"] is None and row["name"] is None
        assert row["removed"] == 1
    # deleted entirely
    assert _row(db, "never-60d") is None
    assert _row(db, "ever-120d") is None


def test_live_configs_are_never_touched_whatever_their_age(db):
    _add(db, "live-ancient", removed=False, days=2000)
    db.prune({**CFG, "removed_compact_days": 0, "removed_delete_days": 0,
              "removed_ever_ok_compact_days": 0, "removed_ever_ok_delete_days": 0})
    assert _row(db, "live-ancient")["outbound"] == '{"type":"vless"}'


def _item(fp):
    return {"fingerprint": fp, "name": "again", "link": "vless://x",
            "outbound": {"type": "vless"}}


def test_compacted_tombstone_still_blocks_reimport_but_deleted_one_does_not(db):
    _seed(db)
    sub = db.add_subscription("https://example.test/sub")
    db.prune(CFG)

    # A subscription that still lists a compacted (not yet deleted) config must
    # not bring it back as a fresh candidate...
    assert db.sync_configs(sub, [_item("never-5d")]) == 0
    assert _row(db, "never-5d")["removed"] == 1
    # ...whereas a config past the delete window is treated as new again.
    assert db.sync_configs(sub, [_item("never-60d")]) == 1


def test_prune_does_not_change_pool_generations(db):
    _seed(db)
    before = (db.pool_generation("a"), db.pool_generation("b"))
    db.prune(CFG)
    assert (db.pool_generation("a"), db.pool_generation("b")) == before


def test_prune_trims_old_logs_only(db):
    with db._lock, db._conn:
        for days in (1, 40):
            db._conn.execute(
                "INSERT INTO test_log(ts, pool, total, ok, failed, removed)"
                " VALUES (datetime('now', ?), 'a', 1, 1, 0, 0)", (f"-{days} days",))
            db._conn.execute(
                "INSERT INTO test_rounds(id, pool, snapshot_generation, started_at, state)"
                " VALUES (?, 'a', 0, datetime('now', ?), 'finished')",
                (f"r{days}", f"-{days} days"))
        for days in (1, 120):
            db._conn.execute(
                "INSERT INTO device_traffic_log(ip, ts) VALUES ('10.0.0.2', datetime('now', ?))",
                (f"-{days} days",))

    result = db.prune(CFG)

    assert result["test_log_deleted"] == 1
    assert result["test_rounds_deleted"] == 1
    assert result["traffic_log_deleted"] == 1
    assert db._conn.execute("SELECT COUNT(*) FROM test_log").fetchone()[0] == 1
    assert db._conn.execute("SELECT id FROM test_rounds").fetchone()["id"] == "r1"
    assert db._conn.execute("SELECT COUNT(*) FROM device_traffic_log").fetchone()[0] == 1


def test_prune_processes_everything_across_batches(db):
    for i in range(450):
        _add(db, f"old-{i}", removed=True, days=60)
        _add(db, f"mid-{i}", removed=True, days=10)

    result = db.prune({**CFG, "batch_size": 100})

    assert result["configs_deleted_never_ok"] == 450
    assert result["configs_compacted_never_ok"] == 450
    remaining = db._conn.execute(
        "SELECT COUNT(*), SUM(outbound='') FROM configs").fetchone()
    assert tuple(remaining) == (450, 450)


def test_dry_run_reports_counts_but_changes_nothing(db):
    _seed(db)
    before = [tuple(r) for r in db._conn.execute(
        "SELECT fingerprint, outbound, link FROM configs ORDER BY fingerprint")]

    result = db.prune(CFG, dry_run=True)

    assert result["configs_deleted_never_ok"] == 1
    assert result["configs_deleted_ever_ok"] == 1
    after = [tuple(r) for r in db._conn.execute(
        "SELECT fingerprint, outbound, link FROM configs ORDER BY fingerprint")]
    assert after == before


def test_vacuum_returns_space_to_the_filesystem(db):
    for i in range(3000):
        _add(db, f"bulk-{i}", removed=True, days=60)
    db.prune(CFG)  # deletes them all, but the file keeps its size

    grown = db.file_bytes()
    result = db.vacuum()

    assert result["before_bytes"] == grown
    assert result["after_bytes"] < grown / 4
