"""Unit tests for the concurrency-safe round state store.

Phase One acceptance criteria covered here:
  - concurrent status updates never overwrite another kind's fields
  - concurrent progress updates on the same kind keep all fields
  - every write leaves a complete, valid JSON file (atomic temp+replace)
  - after a daemon crash, no job stays marked "running" (recover)
  - queued/skipped states record their reason and blocker
"""
from __future__ import annotations

import json
import os
import threading

import pytest

from totalray.round_state import RoundStateStore


class TestRoundStateStore:
    def test_start_progress_finish_lifecycle(self, tmp_path):
        store = RoundStateStore(str(tmp_path / "round_status.json"))
        store.start("pool_a", round_id="abc123", total=10)
        snap = store.snapshot()["pool_a"]
        assert snap["state"] == "running"
        assert snap["running"] is True
        assert snap["round_id"] == "abc123"
        assert snap["items_total"] == 10
        assert snap["items_processed"] == 0

        store.progress("pool_a", processed=4, ok=3, failed=1)
        snap = store.snapshot()["pool_a"]
        assert snap["items_processed"] == 4
        assert snap["items_ok"] == 3
        assert snap["items_failed"] == 1
        assert snap["state"] == "running"

        store.finish("pool_a", success=True)
        snap = store.snapshot()["pool_a"]
        assert snap["state"] == "idle"
        assert snap["running"] is False
        assert snap["finished_at"] is not None
        assert snap["last_error"] is None

    def test_finish_failure_records_error(self, tmp_path):
        store = RoundStateStore(str(tmp_path / "round_status.json"))
        store.start("pool_a")
        store.finish("pool_a", success=False, error="boom")
        snap = store.snapshot()["pool_a"]
        assert snap["state"] == "failed"
        assert snap["last_error"] == "boom"

    def test_concurrent_writers_do_not_clobber_each_other(self, tmp_path):
        """Two threads writing different kinds concurrently must both survive."""
        store = RoundStateStore(str(tmp_path / "round_status.json"))
        errors = []
        barrier = threading.Barrier(2)

        def writer(kind, count):
            barrier.wait()
            try:
                for i in range(30):
                    store.progress(kind, processed=i)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=("pool_a", 30)),
                   threading.Thread(target=writer, args=("pool_b", 30))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        snap = store.snapshot()
        assert "pool_a" in snap and "pool_b" in snap
        assert snap["pool_a"]["items_processed"] == 29
        assert snap["pool_b"]["items_processed"] == 29
        assert snap["pool_a"]["state"] == "running"
        assert snap["pool_b"]["state"] == "running"

    def test_concurrent_updates_to_same_kind_keep_all_fields(self, tmp_path):
        """Progress updates on the same kind must not drop other fields
        (the read-modify-write happens under the store's internal lock)."""
        store = RoundStateStore(str(tmp_path / "round_status.json"))
        store.start("pool_a", round_id="r1", total=100)
        barrier = threading.Barrier(2)

        def updater(field, value):
            barrier.wait()
            for _ in range(20):
                store.progress("pool_a", **{field: value})

        threads = [threading.Thread(target=updater, args=("ok", 5)),
                   threading.Thread(target=updater, args=("failed", 2))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        snap = store.snapshot()["pool_a"]
        assert snap["round_id"] == "r1"
        assert snap["items_total"] == 100
        assert snap["items_ok"] == 5
        assert snap["items_failed"] == 2
        assert snap["state"] == "running"

    def test_writes_are_atomic_valid_json(self, tmp_path):
        """Every write must leave a complete, valid JSON file and no temp files."""
        path = str(tmp_path / "round_status.json")
        store = RoundStateStore(path)
        store.start("pool_a")
        store.progress("pool_a", processed=3)
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["pool_a"]["state"] == "running"
        assert data["pool_a"]["items_processed"] == 3
        leftovers = [p for p in os.listdir(tmp_path) if p.startswith(".round-")]
        assert leftovers == []

    def test_recover_marks_stale_running_as_failed(self, tmp_path):
        """A daemon crash must not leave any job stuck in 'running'."""
        path = str(tmp_path / "round_status.json")
        store = RoundStateStore(path)
        store.start("pool_a", round_id="r1")
        store.start("pool_b")

        # New process opens the same file; nothing recovers automatically.
        fresh = RoundStateStore(path)
        assert fresh.snapshot()["pool_a"]["state"] == "running"

        fresh.recover()
        snap = fresh.snapshot()
        for kind in ("pool_a", "pool_b"):
            assert snap[kind]["state"] == "failed"
            assert snap[kind]["running"] is False
            assert snap[kind]["reason"] == "daemon_restart"
            assert "recovered_at" in snap[kind]

        # Already-idle jobs are untouched.
        store.finish("pool_a", success=True)
        fresh.recover()
        assert fresh.snapshot()["pool_a"]["state"] == "idle"

    def test_skip_records_reason_and_blocked_by(self, tmp_path):
        store = RoundStateStore(str(tmp_path / "round_status.json"))
        store.skip("pool_b", reason="already_running", blocked_by="pool_b")
        snap = store.snapshot()["pool_b"]
        assert snap["state"] == "skipped"
        assert snap["running"] is False
        assert snap["reason"] == "already_running"
        assert snap["blocked_by"] == "pool_b"

    def test_snapshot_is_copied_not_shared(self, tmp_path):
        """Callers mutating a snapshot must not corrupt the store."""
        store = RoundStateStore(str(tmp_path / "round_status.json"))
        store.start("pool_a")
        snap = store.snapshot()
        snap["pool_a"]["state"] = "hacked"
        assert store.snapshot()["pool_a"]["state"] == "running"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
