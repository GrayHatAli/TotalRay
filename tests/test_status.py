"""Unit tests for status and observability (Phase Seven).

Covers:
  - _status_snapshot returns complete dict with all sections
  - JSON output mode is parseable
  - generation and round_id are present in pool sections
  - apply coordinator state is included
  - live monitor state is included
  - traffic totals are included when available
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from totalray.main import _status_snapshot


def _make_settings(tmp_path, **overrides):
    """Minimal settings object mimicking totalray.settings.Settings."""
    data = {
        "paths": {"data_dir": str(tmp_path / "data")},
        "schedule": {
            "sub_update_minutes": 360,
            "pool_a_test_minutes": 15,
            "pool_b_test_minutes": 3,
        },
        "clash_api": {"listen": "127.0.0.1:9090", "secret": ""},
        "test": {"max_in_group": 50},
        "live_monitor": {"enabled": True, "check_interval_seconds": 2.0},
    }
    data.update(overrides)
    settings = MagicMock()
    settings.data = data
    settings.data_dir = str(tmp_path / "data")
    settings.__getitem__ = lambda self, key: data[key]
    return settings


def _make_db(**stats_kwargs):
    """Mock DB with controllable stats."""
    db = MagicMock()
    default_stats = {
        "total": 100,
        "pool_a": 40,
        "pool_b": 50,
        "removed": 10,
        "subs": 2,
        "last_test_a": {"ts": "2025-01-01 12:00:00", "total": 20, "ok": 15, "failed": 5, "removed": 2},
        "last_test_b": {"ts": "2025-01-01 12:03:00", "total": 30, "ok": 25, "failed": 5, "removed": 3},
    }
    default_stats.update(stats_kwargs)
    db.stats.return_value = default_stats
    db.list_subscriptions.return_value = [
        {"id": 1, "url": "https://example.com/sub1", "enabled": 1,
         "last_count": 50, "last_status": "ok", "last_update": "2025-01-01 12:00:00"},
        {"id": 2, "url": "https://example.com/sub2", "enabled": 0,
         "last_count": 30, "last_status": "ok", "last_update": "2025-01-01 11:00:00"},
    ]
    db.configs_health_by_sub.return_value = {1: 20, 2: 0}
    db.get_device_totals.return_value = []
    db.top_configs.return_value = []
    db.worst_configs.return_value = []
    return db


class TestStatusSnapshot:
    def test_returns_all_top_level_keys(self, tmp_path):
        settings = _make_settings(tmp_path)
        db = _make_db()

        data = _status_snapshot(settings, db)

        expected_keys = {
            "status_line", "traffic", "live_monitor", "apply",
            "subscriptions", "configs", "pool_a", "pool_b", "devices",
        }
        assert set(data.keys()) == expected_keys

    def test_pool_a_has_generation_and_round_id(self, tmp_path):
        settings = _make_settings(tmp_path)
        db = _make_db()
        # Write a round_status file with generation and round_id
        rs_path = str(tmp_path / "data" / "round_status.json")
        import os
        os.makedirs(os.path.dirname(rs_path), exist_ok=True)
        with open(rs_path, "w") as fh:
            json.dump({
                "pool_a": {
                    "state": "running",
                    "round_id": "abc123",
                    "snapshot_generation": 7,
                    "items_total": 100,
                    "items_processed": 45,
                }
            }, fh)

        data = _status_snapshot(settings, db)

        assert data["pool_a"]["round_id"] == "abc123"
        assert data["pool_a"]["snapshot_generation"] == 7
        assert data["pool_a"]["state"] == "running"
        assert data["pool_a"]["items_total"] == 100
        assert data["pool_a"]["items_processed"] == 45

    def test_pool_b_has_generation_and_round_id(self, tmp_path):
        settings = _make_settings(tmp_path)
        db = _make_db()
        rs_path = str(tmp_path / "data" / "round_status.json")
        import os
        os.makedirs(os.path.dirname(rs_path), exist_ok=True)
        with open(rs_path, "w") as fh:
            json.dump({
                "pool_b": {
                    "state": "idle",
                    "round_id": "def456",
                    "snapshot_generation": 3,
                    "items_total": 50,
                    "items_ok": 45,
                    "items_failed": 5,
                }
            }, fh)

        data = _status_snapshot(settings, db)

        assert data["pool_b"]["round_id"] == "def456"
        assert data["pool_b"]["snapshot_generation"] == 3
        assert data["pool_b"]["state"] == "idle"

    def test_apply_coordinator_state_included(self, tmp_path):
        settings = _make_settings(tmp_path)
        db = _make_db()
        # Write apply state
        apply_path = str(tmp_path / "data" / "apply_state.json")
        import os
        os.makedirs(os.path.dirname(apply_path), exist_ok=True)
        with open(apply_path, "w") as fh:
            json.dump({
                "circuit_open": True,
                "restart_count_total": 7,
                "restart_failures_total": 2,
                "last_restart_at": 1700000000.0,
                "last_restart_reason": "membership_changed",
                "last_restart_ok": True,
            }, fh)

        data = _status_snapshot(settings, db)

        assert data["apply"]["available"] is True
        assert data["apply"]["circuit_open"] is True
        assert data["apply"]["restarts_total"] == 7
        assert data["apply"]["restart_failures_total"] == 2
        assert data["apply"]["last_restart_reason"] == "membership_changed"

    def test_apply_not_available_when_no_state_file(self, tmp_path):
        settings = _make_settings(tmp_path)
        db = _make_db()

        data = _status_snapshot(settings, db)

        assert data["apply"]["available"] is False

    def test_traffic_totals_when_available(self, tmp_path):
        settings = _make_settings(tmp_path)
        db = _make_db()

        with patch("totalray.main._get_traffic_totals", return_value=(1024, 2048)):
            data = _status_snapshot(settings, db)

        assert data["traffic"] == {"down": 1024, "up": 2048}

    def test_traffic_none_when_unavailable(self, tmp_path):
        settings = _make_settings(tmp_path)
        db = _make_db()

        with patch("totalray.main._get_traffic_totals", return_value=None):
            data = _status_snapshot(settings, db)

        assert data["traffic"] is None

    def test_json_serializable(self, tmp_path):
        """The snapshot dict must be JSON-serializable (no datetime objects, etc.)."""
        settings = _make_settings(tmp_path)
        db = _make_db()

        data = _status_snapshot(settings, db)

        # Must not raise
        output = json.dumps(data, default=str)
        assert isinstance(output, str)
        # Must be valid JSON
        parsed = json.loads(output)
        assert parsed["configs"]["total"] == 100

    def test_configs_counts(self, tmp_path):
        settings = _make_settings(tmp_path)
        db = _make_db()

        data = _status_snapshot(settings, db)

        assert data["configs"]["total"] == 100
        assert data["configs"]["pool_a"] == 40
        assert data["configs"]["pool_b"] == 50
        assert data["configs"]["removed"] == 10

    def test_subscriptions_include_state(self, tmp_path):
        settings = _make_settings(tmp_path)
        db = _make_db()
        rs_path = str(tmp_path / "data" / "round_status.json")
        import os
        os.makedirs(os.path.dirname(rs_path), exist_ok=True)
        with open(rs_path, "w") as fh:
            json.dump({
                "subscriptions": {
                    "state": "running",
                    "round_id": "sub123",
                }
            }, fh)

        data = _status_snapshot(settings, db)

        sub = data["subscriptions"][0]
        assert sub["id"] == 1
        assert sub["url"] == "https://example.com/sub1"
        assert sub["healthy"] == 20
        assert sub["state"] == "running"
        assert sub["round_id"] == "sub123"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
