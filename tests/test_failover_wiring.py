"""Live failover wiring: LiveMonitor -> FailoverController.

The controller expects switch() to return (ok, message), and its circuit
breaker must neither trip on cooldown polls nor stay open forever.
"""
from __future__ import annotations

from unittest.mock import Mock, patch

from totalray.failover import FailoverController
from totalray.live_monitor import LiveMonitor


class _Probe:
    def __init__(self, tag):
        self.tag = tag


def _monitor(**kwargs):
    settings = Mock(data_dir="/tmp/totalray-test")
    settings.data = {"clash_api": {"listen": "127.0.0.1:9090", "secret": ""}}
    monitor = LiveMonitor(settings, Mock(), shadow_mode=False, **kwargs)
    monitor._write_status = Mock()
    return monitor


def test_failover_through_live_monitor_records_the_switch():
    monitor = _monitor(cooldown_seconds=30.0)
    with patch("totalray.builder.set_active_config", return_value=(True, "ok")):
        ok, message = monitor._failover.attempt("cfg-1", [_Probe("cfg-2")], now=100)
    assert ok
    assert message == "switched to cfg-2"
    assert monitor._failover.last_target == "cfg-2"
    assert monitor._failover.last_switch == 100
    assert len(monitor._failover.history) == 1
    # cooldown is now enforced
    with patch("totalray.builder.set_active_config", return_value=(True, "ok")) as switch:
        ok, message = monitor._failover.attempt("cfg-2", [_Probe("cfg-3")], now=110)
    assert not ok
    assert message == "failover cooldown active"
    switch.assert_not_called()


def test_failed_switch_returns_the_reason():
    monitor = _monitor()
    with patch("totalray.builder.set_active_config", return_value=(False, "clash said no")):
        ok, message = monitor._failover.attempt("cfg-1", [_Probe("cfg-2")], now=100)
    assert not ok
    assert message == "clash said no"
    assert monitor._failover.last_target is None


def test_failing_post_failover_callback_does_not_undo_the_switch():
    monitor = _monitor()
    monitor._on_failover = Mock(side_effect=RuntimeError("refill failed"))
    with patch("totalray.builder.set_active_config", return_value=(True, "ok")):
        ok, _ = monitor._failover.attempt("cfg-1", [_Probe("cfg-2")], now=100)
    assert ok
    assert monitor._failover.last_target == "cfg-2"


def test_polls_during_cooldown_do_not_open_the_breaker():
    switch = Mock(return_value=(True, "switched"))
    controller = FailoverController(switch, shadow_mode=False,
                                    cooldown_seconds=30, max_failures=3)
    assert controller.attempt("cfg-1", [_Probe("cfg-2")], now=100)[0]
    # the degraded signal is still on for the next few 2-second polls
    for i in range(10):
        ok, message = controller.attempt("cfg-2", [_Probe("cfg-3")], now=101 + 2 * i)
        assert not ok
        assert message == "failover cooldown active"
    snap = controller.snapshot(now=121)
    assert not snap["circuit_open"]
    assert snap["failures_in_window"] == 0


def test_breaker_closes_after_the_failure_window():
    switch = Mock(return_value=(True, "switched"))
    controller = FailoverController(switch, shadow_mode=False, cooldown_seconds=0,
                                    max_failures=2, failure_window_seconds=300)
    ready = [_Probe("cfg-2"), _Probe("cfg-3"), _Probe("cfg-4"), _Probe("cfg-5")]
    assert controller.attempt("cfg-1", ready, now=100)[0]
    assert controller.attempt("cfg-2", ready, now=101)[0]
    assert not controller.attempt("cfg-3", ready, now=102)[0]
    assert controller.snapshot(now=102)["circuit_open"]
    # still open inside the window
    assert controller.attempt("cfg-3", ready, now=200)[1] == "failover circuit breaker open"
    # once every failure has aged out, failover works again
    assert not controller.snapshot(now=500)["circuit_open"]
    assert controller.attempt("cfg-4", ready, now=500)[0]
