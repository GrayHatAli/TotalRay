"""A failed Pool B test round must not leave Pool B marked "running".

job_test_pool_b skips itself while the round state says "running", so a
round that raised without being closed would block Pool B testing until the
daemon restarted.
"""
from __future__ import annotations

from unittest.mock import patch

from totalray.db import Database
from totalray.scheduler import Manager
from totalray.settings import Settings


def _manager(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "paths:\n"
        f"  data_dir: {tmp_path / 'data'}\n"
        f"  rules_dir: {tmp_path / 'rules'}\n"
        f"  sing_box_config: {tmp_path / 'sing-box.json'}\n")
    settings = Settings(str(cfg))
    settings.ensure_dirs()
    db = Database(settings.db_path)
    sub_id = db.add_subscription("http://example.invalid/sub")
    db.sync_configs(sub_id, [{"fingerprint": "f1", "name": "n1", "link": "",
                              "outbound": {"type": "direct"}}])
    cfg_id = db.get_pool_snapshot("a")["configs"][0]["id"]
    db.record_pool_a_results({cfg_id: 100}, 3000, -5)  # promote into pool B
    assert db.get_pool_snapshot("b")["configs"], "test setup: pool B is empty"
    return Manager(settings, db)


class _ExplodingTester:
    calls = 0
    pools: list = []

    def __init__(self, settings, pool="a"):
        type(self).pools.append(pool)

    def test_all(self, items, on_chunk=None):
        type(self).calls += 1
        raise RuntimeError("sing-box check timed out")


def test_pool_b_round_is_closed_when_testing_raises(tmp_path):
    _ExplodingTester.calls = 0
    _ExplodingTester.pools = []
    mgr = _manager(tmp_path)
    with patch.object(Manager, "_internet_ok", return_value=True), \
         patch("totalray.scheduler.GroupTester", _ExplodingTester):
        mgr.job_test_pool_b()   # the job wrapper swallows the error
        state = mgr._round_state.snapshot()["pool_b"]
        assert state["state"] == "failed"
        assert "timed out" in state["last_error"]

        mgr.job_test_pool_b()   # must run again, not be skipped as "running"
    assert _ExplodingTester.calls == 2
    assert _ExplodingTester.pools == ["b", "b"]
