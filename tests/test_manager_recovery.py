"""Only the daemon may clear rounds left "running" by a crashed process."""
from __future__ import annotations

import os

from totalray.db import Database
from totalray.round_state import RoundStateStore
from totalray.scheduler import Manager
from totalray.settings import Settings


def _setup(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "paths:\n"
        f"  data_dir: {tmp_path / 'data'}\n"
        f"  rules_dir: {tmp_path / 'rules'}\n"
        f"  sing_box_config: {tmp_path / 'sing-box.json'}\n")
    settings = Settings(str(cfg))
    settings.ensure_dirs()
    db = Database(settings.db_path)
    # a round the (pretend) daemon is running right now
    daemon_view = RoundStateStore(os.path.join(settings.data_dir, "round_status.json"))
    daemon_view.start("pool_a", round_id="live")
    return settings, db, daemon_view


def test_creating_a_manager_does_not_touch_running_rounds(tmp_path):
    settings, db, daemon_view = _setup(tmp_path)
    Manager(settings, db)   # what `totalray test-a` / `build` do
    assert daemon_view.snapshot()["pool_a"]["state"] == "running"


def test_daemon_start_recovery_marks_interrupted_rounds_failed(tmp_path):
    settings, db, daemon_view = _setup(tmp_path)
    Manager(settings, db).recover_interrupted_rounds()
    assert daemon_view.snapshot()["pool_a"]["state"] == "failed"
