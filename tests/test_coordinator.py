"""Unit tests for the ApplyCoordinator (Phase Six).

Covers:
  - circuit breaker opens after max restarts in window
  - circuit breaker half-open after window expires
  - cooldown prevents rapid successive restarts
  - force=True bypasses cooldown
  - health check failure is tracked as a failed restart
  - status dict reflects coordinator state
  - state persists and survives process restart
  - reset_circuit clears breaker and timestamps
"""
from __future__ import annotations

import json
import time
from unittest.mock import patch, MagicMock

import pytest

from totalray.coordinator import (
    ApplyCoordinator,
    REASON_MEMBERSHIP_CHANGED,
    REASON_RULES_UPDATED,
)


def _make_settings(tmp_path, **apply_overrides):
    """Minimal settings dict for testing."""
    data_dir = str(tmp_path / "data")
    cfg = {
        "paths": {"data_dir": data_dir},
        "apply": {
            "restart_cooldown_seconds": 2,
            "max_restarts_per_window": 3,
            "restart_window_seconds": 5,
            "health_check_retries": 1,
            "health_check_delay_seconds": 0,
        },
    }
    cfg["apply"].update(apply_overrides)
    return cfg


def _make_db():
    """Stub DB that returns an empty pool."""
    db = MagicMock()
    db.get_pool_configs.return_value = []
    return db


def _patch_builder(ok=True, msg="switched to cfg-1 (no restart)"):
    """Patch builder.rebuild_and_apply to return controlled results."""
    return patch("totalray.coordinator.builder.rebuild_and_apply",
                 return_value=(ok, msg))


def _patch_health(ok=True):
    """Patch the health check to return controlled results."""
    return patch("totalray.coordinator._health_check", return_value=ok)


class TestApplyCoordinator:
    def test_apply_returns_builder_result(self, tmp_path):
        settings = _make_settings(tmp_path)
        db = _make_db()
        coord = ApplyCoordinator(settings, db)

        with _patch_builder(True, "switched (no restart)"):
            ok, msg = coord.apply()

        assert ok is True
        assert "switched" in msg

    def test_apply_tracks_restart_when_needed(self, tmp_path):
        settings = _make_settings(tmp_path)
        db = _make_db()
        coord = ApplyCoordinator(settings, db)

        with _patch_builder(True, "2 configs in group"), _patch_health(True):
            ok, msg = coord.apply(reason="test")

        assert ok is True
        status = coord.status
        assert status["restarts_total"] == 1
        assert status["last_restart_reason"] == "test"
        assert status["last_restart_ok"] is True
        assert status["circuit_open"] is False

    def test_apply_no_restart_for_selector_switch(self, tmp_path):
        settings = _make_settings(tmp_path)
        db = _make_db()
        coord = ApplyCoordinator(settings, db)

        with _patch_builder(True, "switched to cfg-1 (no restart)"):
            ok, msg = coord.apply()

        status = coord.status
        assert status["restarts_total"] == 0
        assert status["last_restart_at"] is None

    def test_circuit_breaker_opens_after_max_restarts(self, tmp_path):
        # cooldown=0 so only the circuit breaker logic is exercised
        settings = _make_settings(tmp_path, max_restarts_per_window=2,
                                  restart_cooldown_seconds=0)
        db = _make_db()
        coord = ApplyCoordinator(settings, db)

        with _patch_builder(True, "1 configs in group"), _patch_health(True):
            coord.apply(reason="r1")
            coord.apply(reason="r2")

        assert coord.status["circuit_open"] is True

    def test_circuit_breaker_blocks_apply(self, tmp_path):
        settings = _make_settings(tmp_path, max_restarts_per_window=2,
                                  restart_cooldown_seconds=0)
        db = _make_db()
        coord = ApplyCoordinator(settings, db)

        with _patch_builder(True, "1 configs in group"), _patch_health(True):
            coord.apply(reason="r1")
            coord.apply(reason="r2")

        # Third apply should be blocked by circuit breaker
        ok, msg = coord.apply(reason="r3")
        assert ok is False
        assert "circuit breaker" in msg

    def test_circuit_breaker_half_open_after_window(self, tmp_path):
        settings = _make_settings(tmp_path, max_restarts_per_window=2,
                                  restart_window_seconds=1,
                                  restart_cooldown_seconds=0)
        db = _make_db()
        coord = ApplyCoordinator(settings, db)

        with _patch_builder(True, "1 configs in group"), _patch_health(True):
            coord.apply(reason="r1")
            coord.apply(reason="r2")

        assert coord.status["circuit_open"] is True

        # Wait for window to expire
        time.sleep(1.1)

        # Now apply should be allowed (half-open)
        with _patch_builder(True, "1 configs in group"), _patch_health(True):
            ok, msg = coord.apply(reason="retry")

        assert ok is True

    def test_cooldown_prevents_rapid_restart(self, tmp_path):
        settings = _make_settings(tmp_path, restart_cooldown_seconds=5)
        db = _make_db()
        coord = ApplyCoordinator(settings, db)

        with _patch_builder(True, "1 configs in group"), _patch_health(True):
            coord.apply(reason="r1")

        # Immediate second apply should be blocked by cooldown
        ok, msg = coord.apply(reason="r2")
        assert ok is False
        assert "cooldown" in msg

    def test_force_bypasses_cooldown(self, tmp_path):
        settings = _make_settings(tmp_path, restart_cooldown_seconds=999)
        db = _make_db()
        coord = ApplyCoordinator(settings, db)

        with _patch_builder(True, "1 configs in group"), _patch_health(True):
            coord.apply(reason="r1")

        # force=True should bypass cooldown
        with _patch_builder(True, "1 configs in group"), _patch_health(True):
            ok, msg = coord.apply(force=True, reason="forced")

        assert ok is True

    def test_health_check_failure_records_failure(self, tmp_path):
        settings = _make_settings(tmp_path)
        db = _make_db()
        coord = ApplyCoordinator(settings, db)

        with _patch_builder(True, "1 configs in group"), _patch_health(False):
            ok, msg = coord.apply(reason="test")

        assert ok is False
        assert "health check" in msg
        status = coord.status
        assert status["restart_failures_total"] == 1
        assert status["last_restart_ok"] is False

    def test_status_dict_has_all_fields(self, tmp_path):
        settings = _make_settings(tmp_path)
        db = _make_db()
        coord = ApplyCoordinator(settings, db)
        status = coord.status

        expected_keys = {
            "restarts_total", "restart_failures_total", "restarts_in_window",
            "last_restart_at", "last_restart_reason", "last_restart_ok",
            "circuit_open", "circuit_open_at",
        }
        assert set(status.keys()) == expected_keys

    def test_state_persists_across_instances(self, tmp_path):
        settings = _make_settings(tmp_path)
        db = _make_db()

        # First instance: record a restart
        coord1 = ApplyCoordinator(settings, db)
        with _patch_builder(True, "1 configs in group"), _patch_health(True):
            coord1.apply(reason="persist_test")

        # Second instance: should load previous state
        coord2 = ApplyCoordinator(settings, db)
        status = coord2.status
        assert status["restarts_total"] == 1
        assert status["last_restart_reason"] == "persist_test"

    def test_reset_circuit_clears_state(self, tmp_path):
        settings = _make_settings(tmp_path, max_restarts_per_window=2,
                                  restart_cooldown_seconds=0)
        db = _make_db()
        coord = ApplyCoordinator(settings, db)

        with _patch_builder(True, "1 configs in group"), _patch_health(True):
            coord.apply(reason="r1")
            coord.apply(reason="r2")

        assert coord.status["circuit_open"] is True
        coord.reset_circuit()
        assert coord.status["circuit_open"] is False
        assert coord.status["restarts_in_window"] == 0

    def test_failed_restart_counts_in_circuit_breaker(self, tmp_path):
        settings = _make_settings(tmp_path, max_restarts_per_window=3,
                                  restart_cooldown_seconds=0)
        db = _make_db()
        coord = ApplyCoordinator(settings, db)

        # Mix of successes and failures
        with _patch_builder(True, "1 configs in group"), _patch_health(True):
            coord.apply(reason="ok1")
        with _patch_builder(False, "restart failed"), _patch_health(True):
            coord.apply(reason="fail1")
        with _patch_builder(True, "1 configs in group"), _patch_health(True):
            coord.apply(reason="ok2")

        # 3 restart attempts in window -> circuit should be open
        assert coord.status["circuit_open"] is True
        assert coord.status["restarts_total"] == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
