"""Focused tests for the embedded-core standby and degradation layers."""
from __future__ import annotations

import time
from unittest.mock import Mock

from totalray.failover import FailoverController
from totalray.health_signal import DegradationDetector, RetransmitSampler
from totalray.singbox_core import SingboxCoreClient
from totalray.standby_pool import StandbyPool
from totalray.standby_probe import StandbyProbe


class FakeCore:
    def __init__(self):
        self.calls = []
        self.running = True

    def is_running(self):
        return self.running

    def add_outbound(self, config):
        self.calls.append(("add", config["tag"]))

    def remove_outbound(self, tag):
        self.calls.append(("remove", tag))


def _config(tags):
    leaves = [{"type": "direct", "tag": tag} for tag in tags]
    return {
        "outbounds": [
            {"type": "selector", "tag": "auto", "outbounds": tags},
            {"type": "selector", "tag": "select", "outbounds": ["auto", "direct"] + tags},
            *leaves,
            {"type": "direct", "tag": "direct"},
        ]
    }


def test_standby_reconcile_adds_before_replacing_groups():
    core = FakeCore()
    pool = StandbyPool(core)
    old = _config(["cfg-1", "cfg-2"])
    new = _config(["cfg-2", "cfg-3"])

    message = pool.reconcile(new, old)

    assert "live pool reconciled" in message
    assert core.calls == [
        ("add", "cfg-3"),
        ("remove", "select"),
        ("remove", "auto"),
        ("add", "auto"),
        ("add", "select"),
        ("remove", "cfg-1"),
    ]


def test_standby_reconcile_refuses_active_leaf_removal():
    core = FakeCore()
    pool = StandbyPool(core)
    try:
        pool.reconcile(_config(["cfg-2"]), _config(["cfg-1", "cfg-2"]), "cfg-1")
    except RuntimeError as exc:
        assert "currently selected" in str(exc)
    else:
        raise AssertionError("active outbound removal was not refused")


def test_degradation_detector_requires_hysteresis_and_active_tag():
    detector = DegradationDetector(error_threshold=2, hysteresis_samples=3)
    detector.set_active_tag("cfg-1")
    assert not detector.observe_log_line("cfg-2: connection reset")
    assert not detector.observe_log_line("cfg-1: connection reset", now=1)
    assert not detector.observe_log_line("cfg-1: timeout", now=2)
    assert not detector.observe_log_line("cfg-1: closed pipe", now=3)
    assert detector.observe_log_line("cfg-1: timeout", now=4)


def test_retransmit_sampler_parses_kernel_counters():
    runner = Mock(return_value=Mock(stdout="ESTAB bytes_retrans:4\nESTAB retrans:2\n"))
    assert RetransmitSampler(runner).sample() == 6


def test_probe_marks_stale_results_unready():
    session = Mock()
    response = Mock()
    response.json.return_value = {"delay": 120}
    response.raise_for_status.return_value = None
    session.get.return_value = response
    probe = StandbyProbe("127.0.0.1:9090", session=session, stale_after=1)
    result = probe.probe("cfg-2")
    assert result.delay_ms == 120
    assert probe.ready(["cfg-2"])
    result.last_success = time.time() - 2
    assert probe.ready(["cfg-2"]) == []


def test_failover_shadow_mode_does_not_switch():
    switch = Mock(return_value=(True, "switched"))
    controller = FailoverController(switch, shadow_mode=True)
    ready = [type("Probe", (), {"tag": "cfg-2"})()]
    ok, message = controller.attempt("cfg-1", ready, now=100)
    assert not ok
    assert "shadow" in message
    switch.assert_not_called()


def test_failover_circuit_breaker_opens_after_promoted_failures():
    switch = Mock(side_effect=[(True, "switched"), (True, "switched"), (True, "switched")])
    controller = FailoverController(switch, shadow_mode=False,
                                    cooldown_seconds=0, max_failures=2)
    ready = [type("Probe", (), {"tag": "cfg-2"})(),
             type("Probe", (), {"tag": "cfg-3"})(),
             type("Probe", (), {"tag": "cfg-4"})()]
    assert controller.attempt("cfg-1", ready, now=100)[0]
    assert controller.attempt("cfg-2", ready, now=101)[0]
    assert not controller.attempt("cfg-3", ready, now=102)[0]
    assert controller.snapshot(now=102)["circuit_open"]


def test_client_is_running_uses_control_response(monkeypatch):
    client = SingboxCoreClient("/tmp/unused")
    monkeypatch.setattr(client, "request", lambda command, **fields: {"ok": True, "running": True})
    assert client.is_running()
