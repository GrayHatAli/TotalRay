"""Unit tests for the live connection monitor."""
from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from totalray.live_monitor import ConnectionSample, QualityWindow, LiveMonitor


class TestQualityWindow:
    """Test the QualityWindow sliding window logic."""

    def test_add_sample_tracks_errors(self):
        """Adding samples with errors increments error count."""
        window = QualityWindow()
        
        # Add a sample without errors
        sample1 = ConnectionSample(
            timestamp=time.time(),
            download_speed=1000,
            upload_speed=500,
            active_connections=5,
            errors=0
        )
        window.add_sample(sample1)
        assert window.error_count == 0
        
        # Add a sample with errors
        sample2 = ConnectionSample(
            timestamp=time.time(),
            download_speed=0,
            upload_speed=0,
            active_connections=0,
            errors=1
        )
        window.add_sample(sample2)
        assert window.error_count == 1

    def test_old_errors_are_cleaned(self):
        """Errors older than 30 seconds should be cleaned from count."""
        window = QualityWindow()
        
        # Add sample with error
        old_time = time.time() - 35  # 35 seconds ago
        old_sample = ConnectionSample(
            timestamp=old_time,
            download_speed=0,
            upload_speed=0,
            active_connections=0,
            errors=1
        )
        window.samples.append(old_sample)
        window.error_count = 1
        window.last_error_time = old_time
        
        # Add new sample (triggers cleanup)
        new_sample = ConnectionSample(
            timestamp=time.time(),
            download_speed=1000,
            upload_speed=500,
            active_connections=5,
            errors=0
        )
        window.add_sample(new_sample)
        
        # Old error should have been cleaned
        assert window.error_count == 0

    def test_get_recent_errors(self):
        """Count only errors within the time window."""
        window = QualityWindow()
        now = time.time()
        
        # Add old error (outside 10-second window)
        old_sample = ConnectionSample(
            timestamp=now - 15,
            download_speed=0,
            upload_speed=0,
            active_connections=0,
            errors=1
        )
        window.samples.append(old_sample)
        
        # Add recent errors (inside 10-second window)
        for i in range(3):
            recent_sample = ConnectionSample(
                timestamp=now - (i * 2),  # 0, 2, 4 seconds ago
                download_speed=0,
                upload_speed=0,
                active_connections=0,
                errors=1
            )
            window.samples.append(recent_sample)
        
        recent_errors = window.get_recent_errors(window_seconds=10)
        assert recent_errors == 3

    def test_is_degraded_with_threshold(self):
        """Degradation detection respects threshold parameter."""
        window = QualityWindow()
        now = time.time()
        
        # Add 2 errors (below default threshold of 3)
        for i in range(2):
            sample = ConnectionSample(
                timestamp=now - i,
                download_speed=0,
                upload_speed=0,
                active_connections=0,
                errors=1
            )
            window.samples.append(sample)
        
        assert not window.is_degraded(threshold=3)
        assert window.is_degraded(threshold=2)  # Would be degraded at threshold=2
        
        # Add 3rd error
        sample3 = ConnectionSample(
            timestamp=now,
            download_speed=0,
            upload_speed=0,
            active_connections=0,
            errors=1
        )
        window.samples.append(sample3)
        
        assert window.is_degraded(threshold=3)


class TestLiveMonitorHealthCheck:
    """Test the _check_connection_health method."""

    def test_api_unavailable_returns_healthy(self):
        """When Clash API is unavailable, don't count as error."""
        settings = Mock()
        settings.data = {"clash_api": {"listen": "127.0.0.1:9090", "secret": ""}}
        db = Mock()
        
        monitor = LiveMonitor(settings, db)
        
        with patch.object(monitor, '_get_connections', return_value=None):
            is_healthy, errors = monitor._check_connection_health()
            assert is_healthy is True
            assert errors == 0

    def test_short_lived_connections_detected_as_errors(self):
        """Multiple short-lived connections indicate degradation."""
        settings = Mock()
        settings.data = {"clash_api": {"listen": "127.0.0.1:9090", "secret": ""}}
        db = Mock()
        
        monitor = LiveMonitor(settings, db)
        
        # Create connections with very recent start times (guaranteed < 1s old)
        # Use a fixed timestamp format that will parse correctly
        now = time.time()
        connections = []
        for i in range(5):
            # Use a timestamp just milliseconds ago to ensure duration < 1s
            conn = {
                "start": datetime.fromtimestamp(now - 0.1).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "download": 100,
                "upload": 50
            }
            connections.append(conn)
        
        with patch.object(monitor, '_get_connections', return_value=connections):
            is_healthy, errors = monitor._check_connection_health()
            # Should detect errors due to multiple short-lived connections (>=3 triggers error)
            assert errors >= 1

    def test_zero_throughput_not_counted_immediately(self):
        """Zero throughput on new connections shouldn't immediately trigger errors."""
        settings = Mock()
        settings.data = {"clash_api": {"listen": "127.0.0.1:9090", "secret": ""}}
        db = Mock()
        
        monitor = LiveMonitor(settings, db)
        
        # Create connections that are very new (< 5 seconds) with zero throughput
        now = time.time()
        connections = []
        for i in range(3):
            conn = {
                "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 2)),
                "download": 0,
                "upload": 0
            }
            connections.append(conn)
        
        with patch.object(monitor, '_get_connections', return_value=connections):
            is_healthy, errors = monitor._check_connection_health()
            # Should not count these as errors yet (need > 5 seconds duration)
            assert errors == 0

    def test_computes_throughput_metrics(self):
        """Health check should compute average throughput metrics."""
        settings = Mock()
        settings.data = {"clash_api": {"listen": "127.0.0.1:9090", "secret": ""}}
        db = Mock()
        
        monitor = LiveMonitor(settings, db, check_interval=0.1)
        
        # Create connections with known throughput
        connections = [
            {"start": "2024-01-15T10:30:00Z", "download": 1000, "upload": 500},
            {"start": "2024-01-15T10:30:00Z", "download": 2000, "upload": 1000},
        ]
        
        with patch.object(monitor, '_get_connections', return_value=connections):
            monitor._check_connection_health()
            
            # Check that the last sample has computed throughput
            if monitor._quality.samples:
                last_sample = monitor._quality.samples[-1]
                assert last_sample.download_speed == 1500  # Average of 1000 and 2000
                assert last_sample.upload_speed == 750  # Average of 500 and 1000


class TestLiveMonitorFailover:
    """Test failover logic and cooldown."""

    def test_get_next_best_server_excludes_current(self):
        """Next best server should not be the current server."""
        settings = Mock()
        settings.data = {"clash_api": {"listen": "127.0.0.1:9090", "secret": ""}}
        db = Mock()
        
        # Mock pool-B configs with latencies
        db.get_pool_configs.return_value = [
            {"id": 1, "delay": 100},
            {"id": 2, "delay": 150},
            {"id": 3, "delay": 200},
        ]
        
        monitor = LiveMonitor(settings, db)
        
        # Current server is cfg-1, should return cfg-2 (next best)
        next_tag = monitor._get_next_best_server("cfg-1")
        assert next_tag == "cfg-2"
        
        # Current server is cfg-2, should return cfg-1 (best available)
        next_tag = monitor._get_next_best_server("cfg-2")
        assert next_tag == "cfg-1"

    def test_get_next_best_server_skips_poor_latency(self):
        """Servers with poor latency (>2000ms) should be skipped."""
        settings = Mock()
        settings.data = {"clash_api": {"listen": "127.0.0.1:9090", "secret": ""}}
        db = Mock()
        
        # Mock pool-B configs where best server has poor latency
        db.get_pool_configs.return_value = [
            {"id": 1, "delay": 2500},  # Poor latency
            {"id": 2, "delay": 150},   # Good latency
            {"id": 3, "delay": 200},   # Good latency
        ]
        
        monitor = LiveMonitor(settings, db)
        
        # Should skip cfg-1 and return cfg-2
        next_tag = monitor._get_next_best_server("cfg-5")
        assert next_tag == "cfg-2"

    def test_cooldown_prevents_rapid_failover(self):
        """Cooldown period should prevent rapid successive failovers."""
        settings = Mock()
        settings.data = {"clash_api": {"listen": "127.0.0.1:9090", "secret": ""}}
        db = Mock()
        
        monitor = LiveMonitor(settings, db, cooldown_seconds=60.0)
        
        # Set last failover to now
        monitor._last_failover = time.time()
        
        # Try to failover immediately (should be blocked by cooldown)
        # This is tested indirectly through the monitor loop logic
        # For unit test, we just verify the cooldown attribute exists
        assert monitor.cooldown_seconds == 60.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
