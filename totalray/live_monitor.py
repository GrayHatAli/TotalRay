"""Live connection monitor for TotalRay with automatic failover.

This module provides real-time monitoring of active connections through
the currently selected sing-box outbound, detecting packet drops and
connection failures, and triggering immediate failover to backup servers.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import requests

log = logging.getLogger(__name__)


@dataclass
class ConnectionSample:
    """A single sample of connection quality metrics."""
    timestamp: float
    download_speed: int  # bytes/sec
    upload_speed: int  # bytes/sec
    active_connections: int
    errors: int = 0


@dataclass
class QualityWindow:
    """Sliding window of connection quality samples."""
    samples: deque = field(default_factory=lambda: deque(maxlen=10))
    error_count: int = 0
    last_error_time: float = 0.0

    def add_sample(self, sample: ConnectionSample) -> None:
        self.samples.append(sample)
        if sample.errors > 0:
            self.error_count += sample.errors
            self.last_error_time = sample.timestamp
        # Clean old errors (older than 30 seconds)
        cutoff = time.time() - 30
        while self.samples and self.samples[0].timestamp < cutoff:
            old_sample = self.samples.popleft()
            if old_sample.timestamp < cutoff and old_sample.errors > 0:
                self.error_count = max(0, self.error_count - old_sample.errors)

    def add_error(self) -> None:
        self.error_count += 1
        self.last_error_time = time.time()

    def get_recent_errors(self, window_seconds: float = 10) -> int:
        """Count errors in the last N seconds."""
        if not self.samples:
            return 0
        cutoff = time.time() - window_seconds
        return sum(1 for s in self.samples if s.timestamp > cutoff and s.errors > 0)

    def is_degraded(self, threshold: int = 3) -> bool:
        """Check if connection quality is degraded based on recent errors."""
        recent_errors = self.get_recent_errors(10)
        return recent_errors >= threshold


class LiveMonitor:
    """Monitor live connection quality and trigger failover on degradation.

    This runs continuously in the background, polling the Clash API for
    connection statistics and detecting issues like:
    - Sudden drops in throughput during active streaming
    - Connection timeouts or failures
    - Packet loss indicators

    When degradation is detected, it immediately switches to the next
    best server in the verified pool (pool-B).
    """

    def __init__(
        self,
        settings,
        db,
        check_interval: float = 2.0,
        error_threshold: int = 3,
        cooldown_seconds: float = 60.0,
    ):
        self.settings = settings
        self.db = db
        self.check_interval = check_interval
        self.error_threshold = error_threshold
        self.cooldown_seconds = cooldown_seconds

        self._clash_host = settings.data["clash_api"]["listen"]
        self._clash_secret = settings.data["clash_api"].get("secret", "")
        self._headers = {}
        if self._clash_secret:
            self._headers["Authorization"] = f"Bearer {self._clash_secret}"

        self._running = False
        self._thread: threading.Thread | None = None
        self._quality = QualityWindow()
        self._last_failover = 0.0
        self._failover_count = 0
        self._consecutive_errors = 0
        self._on_failover: Callable[[str], None] | None = None
        self._started_at = 0.0
        self._last_status_write = 0.0
        data_dir = getattr(settings, "data_dir", "/var/lib/totalray")
        self._status_path = os.path.join(data_dir, "live_monitor_status.json")

    def _write_status(self, extra: dict | None = None) -> None:
        """Write a small JSON snapshot to disk so a separate short-lived
        CLI process (totalray status) can report on this thread's
        state without needing IPC into the running daemon.
        """
        now = time.time()
        payload = {
            "running": self._running,
            "started_at": self._started_at or None,
            "last_check": now,
            "failover_count": self._failover_count,
            "last_failover": self._last_failover or None,
            "consecutive_errors": self._consecutive_errors,
            "check_interval": self.check_interval,
        }
        if extra:
            payload.update(extra)
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(self._status_path) or ".", prefix=".lm-")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            # tempfile.mkstemp always creates the file mode 0600 regardless of
            # umask; match the rest of /var/lib/totalray (e.g. totalray.db,
            # totalray.log) which are group-readable, so status checks work
            # for anyone in the totalray group, not just root/sudo.
            os.chmod(tmp_path, 0o664)
            os.replace(tmp_path, self._status_path)
        except OSError as exc:
            log.debug("could not write live-monitor status file: %s", exc)
        self._last_status_write = now

    def set_failover_callback(self, callback: Callable[[str], None]) -> None:
        """Set callback to be called when failover occurs.

        The callback receives the tag of the new active server.
        """
        self._on_failover = callback

    def _api_get(self, endpoint: str) -> dict | None:
        """Make a GET request to the Clash API."""
        url = f"http://{self._clash_host}{endpoint}"
        try:
            resp = requests.get(url, headers=self._headers, timeout=3)
            if resp.status_code == 200:
                return resp.json()
        except (requests.RequestException, OSError, ValueError) as exc:
            log.debug("Clash API error (%s): %s", endpoint, exc)
        return None

    def _get_proxy_info(self) -> dict | None:
        """Get current proxy selection info from Clash API."""
        data = self._api_get("/proxies")
        if not data:
            return None

        proxies = data.get("proxies", {})
        auto_proxy = proxies.get("auto", {})
        if not auto_proxy:
            return None

        return {
            "now": auto_proxy.get("now"),
            "all": auto_proxy.get("all", []),
        }

    def _get_connections(self) -> list | None:
        """Get active connections from Clash API."""
        data = self._api_get("/connections")
        if not data:
            return None
        return data.get("connections", [])

    def _check_connection_health(self) -> tuple[bool, int]:
        """Check if current connection is healthy.

        Returns:
            Tuple of (is_healthy, error_count)
        """
        connections = self._get_connections()
        if connections is None:
            # API unavailable, don't count as error
            return True, 0

        now = time.time()
        error_count = 0
        active_count = len(connections)
        total_download = 0
        total_upload = 0

        # Analyze connection patterns - check last 50 connections
        short_lived_count = 0
        
        for conn in connections[-50:]:
            # Look for connections that closed very quickly (< 1s)
            # which often indicates connection failures
            start_time_str = conn.get("start", "")
            duration = 0.0
            if start_time_str:
                try:
                    # Parse ISO timestamp (e.g., "2024-01-15T10:30:00Z")
                    start_dt = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                    start_ts = start_dt.timestamp()
                    duration = now - start_ts
                    
                    # Connection lasting less than 1 second with data transfer
                    # likely indicates a failed/retried connection
                    if duration < 1.0:
                        short_lived_count += 1
                        if short_lived_count >= 3:  # Multiple short-lived = degradation
                            error_count += 1
                except (ValueError, TypeError, AttributeError):
                    # Can't parse timestamp, skip duration check
                    pass

            # Accumulate per-connection throughput for the sample
            download = conn.get("download", 0) or 0
            upload = conn.get("upload", 0) or 0
            total_download += download
            total_upload += upload
            

        # Create sample with computed metrics
        sample = ConnectionSample(
            timestamp=now,
            download_speed=total_download // max(1, active_count),
            upload_speed=total_upload // max(1, active_count),
            active_connections=active_count,
            errors=error_count,
        )
        self._quality.add_sample(sample)

        if error_count > 0:
            self._quality.add_error()
            self._consecutive_errors += 1
        else:
            self._consecutive_errors = max(0, self._consecutive_errors - 1)

        # Use configurable threshold for degradation detection
        threshold = self.error_threshold
        is_healthy = not self._quality.is_degraded(threshold)
        return is_healthy, error_count

    def _get_next_best_server(self, current_tag: str) -> str | None:
        """Get the next best server from pool-B, excluding current."""
        configs = self.db.get_pool_configs("b", max_n=10)
        for cfg in configs:
            cfg_tag = f"cfg-{cfg['id']}"
            if cfg_tag == current_tag:
                continue
            # Skip if this config has poor recent performance
            if cfg.get("delay") and (cfg["delay"] <= 0 or cfg["delay"] > 2000):
                continue
            return cfg_tag
        return None

    def _do_failover(self, new_tag: str) -> bool:
        """Switch to a new server via Clash API."""
        from . import builder

        ok, msg = builder.set_active_config(self.settings, new_tag)
        if ok:
            now = time.time()
            self._last_failover = now
            self._failover_count += 1
            self._quality = QualityWindow()  # Reset quality tracking
            self._consecutive_errors = 0
            log.warning(
                "LIVE FAILOVER: switched from previous server to %s "
                "(failover #%d)",
                new_tag,
                self._failover_count,
            )
            self._write_status({"last_target": new_tag})
            if self._on_failover:
                self._on_failover(new_tag)
            return True
        log.error("Live failover to %s failed: %s", new_tag, msg)
        self._write_status({"last_failover_error": msg})
        return False

    def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        log.info("Live connection monitor started (interval=%ss)", self.check_interval)

        while self._running:
            try:
                if time.time() - self._last_status_write >= 10:
                    self._write_status()

                # Get current server
                proxy_info = self._get_proxy_info()
                if not proxy_info:
                    time.sleep(self.check_interval)
                    continue

                current_tag = proxy_info.get("now")
                if not current_tag:
                    time.sleep(self.check_interval)
                    continue

                # Check connection health
                is_healthy, errors = self._check_connection_health()

                if not is_healthy:
                    # Check cooldown
                    now = time.time()
                    if now - self._last_failover < self.cooldown_seconds:
                        log.debug(
                            "Connection degraded but in cooldown (%.1fs remaining)",
                            self.cooldown_seconds - (now - self._last_failover),
                        )
                        time.sleep(self.check_interval)
                        continue

                    # Find and switch to next best server
                    next_tag = self._get_next_best_server(current_tag)
                    if next_tag:
                        log.warning(
                            "Connection degradation detected (errors=%d in window), "
                            "attempting failover from %s to %s",
                            errors,
                            current_tag,
                            next_tag,
                        )
                        self._do_failover(next_tag)
                    else:
                        log.warning(
                            "Connection degradation detected but no suitable "
                            "fallback available in pool-B"
                        )

                time.sleep(self.check_interval)

            except Exception as exc:
                log.exception("Error in monitor loop: %s", exc)
                time.sleep(self.check_interval)

        self._write_status()
        log.info("Live connection monitor stopped")

    def start(self) -> None:
        """Start the monitoring thread."""
        if self._running:
            return
        self._running = True
        self._started_at = time.time()
        self._write_status()
        self._thread = threading.Thread(
            target=self._monitor_loop, name="LiveMonitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the monitoring thread."""
        self._running = False
        self._write_status()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

def create_monitor(settings, db) -> LiveMonitor:
    """Create a LiveMonitor instance with configured settings."""
    lm_cfg = settings.data.get("live_monitor", {})
    
    # Use tighter thresholds for streaming-sensitive use cases
    return LiveMonitor(
        settings=settings,
        db=db,
        check_interval=float(lm_cfg.get("check_interval_seconds", 2.0)),
        error_threshold=int(lm_cfg.get("error_threshold", 3)),
        cooldown_seconds=float(lm_cfg.get("cooldown_seconds", 60.0)),
    )
