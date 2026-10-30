"""Unit tests for the ApplyCoordinator (Phase Six).

Covers:
  - circuit breaker opens after max restarts in window
  - circuit breaker half-open after window expires
  - rapid applies are not blocked (circuit breaker is the safety net)
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

from totalray import builder
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


def _patch_builder_honouring_restart_block(restart_msg="1 configs in group",
                                           no_restart_msg="switched to cfg-1 (no restart)",
                                           needs_restart=True):
    """Like the real builder: a change that needs a restart is refused when
    allow_restart is False; a selector switch is always allowed."""
    def fake(settings, db, force=False, allow_restart=True):
        if needs_restart:
            if not allow_restart:
                return False, builder.RESTART_BLOCKED_MSG
            return True, restart_msg
        return True, no_restart_msg
    return patch("totalray.coordinator.builder.rebuild_and_apply", side_effect=fake)


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

        # Third apply needs a restart, so the open breaker blocks it
        with _patch_builder_honouring_restart_block(), _patch_health(True):
            ok, msg = coord.apply(reason="r3")
        assert ok is False
        assert "circuit breaker" in msg
        assert coord.status["restarts_total"] == 2  # blocked != attempted

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

    def test_rapid_applies_not_blocked(self, tmp_path):
        """Membership changes (Pool A chunk commits) must not be blocked
        by a cooldown -- the circuit breaker alone prevents restart loops.
        """
        settings = _make_settings(tmp_path, restart_cooldown_seconds=5)
        db = _make_db()
        coord = ApplyCoordinator(settings, db)

        with _patch_builder(True, "1 configs in group"), _patch_health(True):
            coord.apply(reason="r1")

        # Second apply should succeed immediately (no cooldown)
        with _patch_builder(True, "1 configs in group"), _patch_health(True):
            ok, msg = coord.apply(reason="r2")

        assert ok is True
        assert coord.status["restarts_total"] == 2

    def test_force_bypasses_cooldown(self, tmp_path):
        settings = _make_settings(tmp_path, restart_cooldown_seconds=999)
        db = _make_db()
        coord = ApplyCoordinator(settings, db)

        with _patch_builder(True, "1 configs in group"), _patch_health(True):
            coord.apply(reason="r1")

        # force=True should always delegate to builder
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
        with _patch_builder(False, f"{builder.RESTART_FAILED_PREFIX}: boom"), _patch_health(True):
            coord.apply(reason="fail1")
        with _patch_builder(True, "1 configs in group"), _patch_health(True):
            coord.apply(reason="ok2")

        # 3 restart attempts in window -> circuit should be open
        assert coord.status["circuit_open"] is True
        assert coord.status["restarts_total"] == 3


class TestBreakerCountsOnlyRestarts:
    def test_rejected_config_is_not_a_restart(self, tmp_path):
        """A config that fails `sing-box check` restarts nothing."""
        settings = _make_settings(tmp_path, max_restarts_per_window=3,
                                  restart_cooldown_seconds=0)
        coord = ApplyCoordinator(settings, _make_db())

        with _patch_builder(False, "FATAL: unknown outbound type"):
            for i in range(6):
                ok, _ = coord.apply(reason=f"r{i}")
                assert ok is False

        status = coord.status
        assert status["restarts_total"] == 0
        assert status["restart_failures_total"] == 0
        assert status["circuit_open"] is False

    def test_failed_health_check_is_one_restart_not_two(self, tmp_path):
        settings = _make_settings(tmp_path)
        coord = ApplyCoordinator(settings, _make_db())

        with _patch_builder(True, "1 configs in group"), _patch_health(False):
            ok, _ = coord.apply(reason="test")

        status = coord.status
        assert ok is False
        assert status["restarts_total"] == 1
        assert status["restarts_in_window"] == 1
        assert status["restart_failures_total"] == 1
        assert status["last_restart_ok"] is False
        assert status["last_restart_reason"] == "test_health_fail"

    def test_open_breaker_still_allows_selector_switches(self, tmp_path):
        settings = _make_settings(tmp_path, max_restarts_per_window=2,
                                  restart_cooldown_seconds=0)
        coord = ApplyCoordinator(settings, _make_db())
        with _patch_builder(True, "1 configs in group"), _patch_health(True):
            coord.apply(reason="r1")
            coord.apply(reason="r2")
        assert coord.status["circuit_open"] is True

        with _patch_builder_honouring_restart_block(needs_restart=False):
            ok, msg = coord.apply(reason="switch")

        assert ok is True
        assert "no restart" in msg
        assert coord.status["restarts_total"] == 2

    def test_open_breaker_blocks_a_change_that_needs_a_restart(self, tmp_path):
        settings = _make_settings(tmp_path, max_restarts_per_window=2,
                                  restart_cooldown_seconds=0)
        coord = ApplyCoordinator(settings, _make_db())
        with _patch_builder(True, "1 configs in group"), _patch_health(True):
            coord.apply(reason="r1")
            coord.apply(reason="r2")

        with _patch_builder_honouring_restart_block(), _patch_health(True):
            ok, msg = coord.apply(reason="needs-restart")

        assert ok is False
        assert "circuit breaker" in msg


class TestBuilderMessageContract:
    """restart_attempted() must agree with what the real builder returns."""

    SETTINGS = {"test": {"max_in_group": 3}, "proxy_group": {"standby_count": 3}}

    def _db(self):
        db = MagicMock()
        db.get_pool_configs.return_value = [{"id": 1}]
        return db

    def _run(self, *, last_tags=None, check=(True, "/x"), reload=(True, "restarted"),
             allow_restart=True):
        patches = {
            "build_config": {"return_value": {}},
            "_load_last_tags": {"return_value": last_tags},
            "_load_live_config": {"return_value": None},
            "write_and_check": {"return_value": check},
            "_apply_live_config": {"return_value": None},
            "reload_singbox": {"return_value": reload},
            "_save_last_tags": {},
            "_save_live_config": {},
            "set_active_config": {"return_value": (True, "switched")},
        }
        mocks = {}
        stack = [patch(f"totalray.builder.{name}", **kw) for name, kw in patches.items()]
        for name, p in zip(patches, stack):
            mocks[name] = p.start()
        try:
            result = builder.rebuild_and_apply(
                self.SETTINGS, self._db(), allow_restart=allow_restart)
        finally:
            for p in stack:
                p.stop()
        return result, mocks

    def test_selector_switch_is_not_a_restart(self):
        (ok, msg), _ = self._run(last_tags={"cfg-1"})
        assert ok and not builder.restart_attempted(ok, msg)

    def test_rejected_config_is_not_a_restart(self):
        (ok, msg), mocks = self._run(check=(False, "FATAL: bad outbound"))
        assert not ok and not builder.restart_attempted(ok, msg)
        mocks["reload_singbox"].assert_not_called()

    def test_successful_restart_is_a_restart(self):
        (ok, msg), _ = self._run()
        assert ok and builder.restart_attempted(ok, msg)

    def test_failed_restart_is_a_restart(self):
        (ok, msg), _ = self._run(reload=(False, "stop failed"))
        assert not ok and builder.restart_attempted(ok, msg)

    def test_blocked_restart_is_not_attempted_and_does_not_restart(self):
        (ok, msg), mocks = self._run(allow_restart=False)
        assert not ok and msg == builder.RESTART_BLOCKED_MSG
        assert not builder.restart_attempted(ok, msg)
        mocks["reload_singbox"].assert_not_called()

    def test_blocked_restart_still_allows_selector_switch(self):
        (ok, msg), _ = self._run(last_tags={"cfg-1"}, allow_restart=False)
        assert ok and not builder.restart_attempted(ok, msg)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
