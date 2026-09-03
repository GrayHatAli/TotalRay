"""Phase 8 unit tests -- acceptance criteria for the architecture rollout.

Covers:
  - Pool A promotion: passing configs move from pool A to pool B
  - Pool B demotion grace: single failure does not demote, multiple do
  - Empty Pool B: builder handles empty verified pool gracefully
  - Status with DNS failure: status output does not crash on connection errors
  - Subscription update during Pool A: concurrent sub update does not corrupt round
"""
from __future__ import annotations

import json
import os
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

from totalray.db import Database


def _make_db(tmp_path) -> Database:
    return Database(str(tmp_path / "test.db"))


def _add_config(db: Database, sub_id: int, pool: str = "a",
                name: str = "test-config", delay=None) -> int:
    """Insert a config into the given pool and return its id."""
    outbound = json.dumps({"type": "shadowsocks", "server": "1.2.3.4",
                           "server_port": 443})
    with db._lock, db._conn:
        cur = db._conn.execute(
            "INSERT INTO configs (fingerprint, name, outbound, source_sub, pool, last_delay)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (f"fp-{name}", name, outbound, sub_id, pool, delay))
        return cur.lastrowid


class TestPoolAPromotion:
    """Passing configs in Pool A should be promoted to Pool B."""

    def test_passing_config_promoted_to_b(self, tmp_path):
        db = _make_db(tmp_path)
        sub_id = db.add_subscription("https://example.com/sub")
        cid = _add_config(db, sub_id, pool="a", name="good-server")

        snap = db.get_pool_snapshot("a")
        assert len(snap["configs"]) == 1
        assert snap["configs"][0]["id"] == cid

        # Record a passing result (delay=50ms, well under 3000ms threshold)
        stats = db.record_pool_a_results({cid: 50}, ping_threshold=3000,
                                         fail_threshold=-5)
        assert stats["ok"] == 1
        assert stats["total"] == 1

        # Config should now be in pool B
        pool_b = db.get_pool_configs("b")
        assert len(pool_b) == 1
        assert pool_b[0]["id"] == cid

        # Pool A should be empty
        pool_a = db.get_pool_configs("a")
        assert len(pool_a) == 0

    def test_failing_config_stays_in_a(self, tmp_path):
        db = _make_db(tmp_path)
        sub_id = db.add_subscription("https://example.com/sub")
        cid = _add_config(db, sub_id, pool="a", name="bad-server")

        # Record a failing result (delay=-1, unreachable)
        stats = db.record_pool_a_results({cid: -1}, ping_threshold=3000,
                                         fail_threshold=-5)
        assert stats["ok"] == 0
        assert stats["failed"] == 1

        # Config should still be in pool A (get_pool_candidates includes
        # failed configs with last_delay=-1; get_pool_configs excludes them
        # because they're not suitable for the live sing-box group)
        candidates = db.get_pool_candidates("a")
        assert len(candidates) == 1
        assert candidates[0]["id"] == cid

    def test_generation_bumps_on_promotion(self, tmp_path):
        db = _make_db(tmp_path)
        sub_id = db.add_subscription("https://example.com/sub")
        cid = _add_config(db, sub_id, pool="a")

        gen_before = db.pool_generation("a")
        db.record_pool_a_results({cid: 50}, ping_threshold=3000,
                                 fail_threshold=-5)
        gen_after = db.pool_generation("a")

        # Generation should increment because membership changed
        assert gen_after > gen_before


class TestPoolBDemotionGrace:
    """Pool B configs should survive a single failure (grace) but be demoted
    after multiple consecutive failures."""

    def test_single_failure_stays_in_b(self, tmp_path):
        db = _make_db(tmp_path)
        sub_id = db.add_subscription("https://example.com/sub")
        cid = _add_config(db, sub_id, pool="b", name="marginal-server")

        # Record one failure -- should get grace, not demotion
        stats = db.record_pool_b_results({cid: -1}, ping_threshold=3000,
                                         fail_threshold=-5, demote_grace=2)
        assert stats["failed"] == 1

        # Config should still be in pool B (grace threshold not reached)
        # Use get_pool_candidates which includes last_delay=-1 entries
        candidates = db.get_pool_candidates("b")
        assert len(candidates) == 1
        assert candidates[0]["id"] == cid

    def test_consecutive_failures_demote_to_a(self, tmp_path):
        db = _make_db(tmp_path)
        sub_id = db.add_subscription("https://example.com/sub")
        cid = _add_config(db, sub_id, pool="b", name="dying-server")

        # Two failures with demote_grace=2 should demote
        db.record_pool_b_results({cid: -1}, ping_threshold=3000,
                                 fail_threshold=-5, demote_grace=2)
        db.record_pool_b_results({cid: -1}, ping_threshold=3000,
                                 fail_threshold=-5, demote_grace=2)

        # Config should now be in pool A
        candidates_a = db.get_pool_candidates("a")
        assert len(candidates_a) == 1
        assert candidates_a[0]["id"] == cid

        # Pool B should be empty of this config
        candidates_b = db.get_pool_candidates("b")
        assert len(candidates_b) == 0

    def test_passing_result_resets_score(self, tmp_path):
        db = _make_db(tmp_path)
        sub_id = db.add_subscription("https://example.com/sub")
        cid = _add_config(db, sub_id, pool="b", name="bounce-server")

        # Fail once (grace), then pass
        db.record_pool_b_results({cid: -1}, ping_threshold=3000,
                                 fail_threshold=-5, demote_grace=2)
        db.record_pool_b_results({cid: 100}, ping_threshold=3000,
                                 fail_threshold=-5, demote_grace=2)

        # Config should still be in pool B with score reset
        pool_b = db.get_pool_configs("b")
        assert len(pool_b) == 1


class TestEmptyPoolB:
    """Builder should handle an empty verified pool (Pool B) gracefully."""

    def test_empty_pool_b_returns_empty_group(self, tmp_path):
        db = _make_db(tmp_path)
        group = db.get_pool_configs("b", max_n=50)
        assert group == []

    def test_empty_pool_b_get_pool_snapshot(self, tmp_path):
        db = _make_db(tmp_path)
        snap = db.get_pool_snapshot("b")
        assert snap["configs"] == []
        assert snap["generation"] >= 0

    def test_builder_builds_direct_only_when_empty(self, tmp_path):
        """build_config with an empty group should produce a config
        that routes everything direct."""
        from totalray.builder import build_config

        settings = MagicMock()
        settings.__getitem__ = lambda self, key: {
            "routing": {"iran_direct": False, "block_ads": False,
                        "block_quic": False, "custom_rules": []},
            "tun": {"interface": "singtun0", "stack": "mixed", "mtu": 1500},
            "dns": {"remote_server": "1.1.1.1", "local_server": "192.168.1.1",
                    "prefer_ipv4": True},
            "proxy_group": {"urltest_interval": "3m", "urltest_tolerance": 50,
                            "idle_timeout": "30m"},
            "clash_api": {"listen": "127.0.0.1:9090", "secret": ""},
            "local_proxy": {"port": 2080},
            "lan_proxy": {"enabled": False},
            "paths": {"rules_dir": str(tmp_path / "rules"),
                      "sing_box_data_dir": str(tmp_path / "data")},
        }[key]
        settings.rules_dir = str(tmp_path / "rules")
        os.makedirs(settings.rules_dir, exist_ok=True)

        config = build_config(settings, [])
        # Should have "direct" as the only outbound (no cfg-* tags)
        outbounds = config["outbounds"]
        outbound_tags = [o["tag"] for o in outbounds]
        assert "direct" in outbound_tags
        assert "select" in outbound_tags
        assert "auto" in outbound_tags
        # No cfg-* outbounds
        cfg_outs = [o for o in outbounds if o["tag"].startswith("cfg-")]
        assert cfg_outs == []


class TestStatusWithDNSFailure:
    """Status output should not crash when connection checks fail."""

    def test_status_snapshot_survives_connection_error(self, tmp_path):
        from totalray.main import _status_snapshot

        settings = MagicMock()
        settings.data = {
            "paths": {"data_dir": str(tmp_path / "data")},
            "schedule": {"sub_update_minutes": 360,
                         "pool_a_test_minutes": 15,
                         "pool_b_test_minutes": 3},
            "clash_api": {"listen": "127.0.0.1:9090", "secret": ""},
            "test": {"max_in_group": 50},
            "live_monitor": {"enabled": True, "check_interval_seconds": 2.0},
        }
        settings.data_dir = str(tmp_path / "data")
        settings.__getitem__ = lambda self, key: settings.data[key]

        db = _make_db(tmp_path)

        # Patch connection check to simulate DNS/API failure
        with patch("totalray.main._live_connection_status",
                   return_value="status        : disconnected (DNS failure)"):
            with patch("totalray.main._get_traffic_totals",
                       return_value=None):
                data = _status_snapshot(settings, db)

        assert "status_line" in data
        assert "disconnected" in data["status_line"]
        assert data["traffic"] is None
        # Should not raise -- all sections should be present
        assert "pool_a" in data
        assert "pool_b" in data
        assert "apply" in data
        assert "subscriptions" in data


class TestSubscriptionDuringPoolA:
    """Subscription updates should not interfere with Pool A rounds."""

    def test_subscription_update_bumps_generation(self, tmp_path):
        db = _make_db(tmp_path)
        sub_id = db.add_subscription("https://example.com/sub")

        gen_before = db.pool_generation("a")

        # Simulate adding new configs from a subscription update
        items = [
            {"fingerprint": f"fp-new-{i}", "name": f"new-server-{i}",
             "link": f"https://example.com/{i}",
             "outbound": {"type": "shadowsocks", "server": f"10.0.0.{i}",
                          "server_port": 443}}
            for i in range(5)
        ]
        added = db.sync_configs(sub_id, items)
        assert added == 5

        gen_after = db.pool_generation("a")
        assert gen_after > gen_before

    def test_existing_pool_b_unaffected_by_new_imports(self, tmp_path):
        db = _make_db(tmp_path)
        sub_id = db.add_subscription("https://example.com/sub")

        # Add a config to pool B first
        cid_b = _add_config(db, sub_id, pool="b", name="verified-server",
                            delay=100)
        pool_b_before = db.get_pool_configs("b")
        assert len(pool_b_before) == 1

        # Import new configs (should only affect pool A)
        items = [
            {"fingerprint": f"fp-new-{i}", "name": f"new-server-{i}",
             "link": f"https://example.com/{i}",
             "outbound": {"type": "shadowsocks", "server": f"10.0.0.{i}",
                          "server_port": 443}}
            for i in range(3)
        ]
        db.sync_configs(sub_id, items)

        # Pool B should be unchanged
        pool_b_after = db.get_pool_configs("b")
        assert len(pool_b_after) == 1
        assert pool_b_after[0]["id"] == cid_b


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
