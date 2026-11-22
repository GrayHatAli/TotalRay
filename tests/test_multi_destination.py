"""Multi-destination health: a server must answer a majority of destinations.

A server that reaches Google but not Cloudflare or Telegram is not usable for
the real traffic mix, yet used to count as "verified" when only one URL was
checked (see DNS_DETOUR_AND_MULTI_PROBE_PLAN.md, Phase 3).
"""
from __future__ import annotations

import time
from unittest.mock import Mock

import pytest
import requests

from totalray import tester
from totalray.live_monitor import create_monitor
from totalray.settings import Settings
from totalray.standby_probe import StandbyProbe
from totalray.tester import GroupTester, measure_multi

PRIMARY = "https://primary.test/"
CF = "https://cloudflare.test/"
TG = "https://telegram.test/"


class FakeMeasure:
    """Stands in for tester.measure: per-URL outcomes plus a call log."""

    def __init__(self, answers):
        self.answers = answers          # url -> delay (ms) or -1
        self.calls = []                 # (url, timeout, retries)

    def __call__(self, port, url, timeout, retries):
        self.calls.append((url, timeout, retries))
        return self.answers[url]


@pytest.fixture
def fake(monkeypatch):
    def install(answers):
        fm = FakeMeasure(answers)
        monkeypatch.setattr(tester, "measure", fm)
        return fm
    return install


def test_primary_failure_runs_no_extra_url(fake):
    fm = fake({PRIMARY: -1, CF: 50, TG: 50})
    assert measure_multi(1, PRIMARY, [CF, TG], 10, 1, 2) == -1
    assert [c[0] for c in fm.calls] == [PRIMARY]


def test_majority_reached_stops_early_and_returns_primary_delay(fake):
    fm = fake({PRIMARY: 321, CF: 50, TG: 50})
    assert measure_multi(1, PRIMARY, [CF, TG], 10, 1, 2) == 321
    # primary + cloudflare already make 2 of 3: telegram is never run
    assert [c[0] for c in fm.calls] == [PRIMARY, CF]


def test_second_extra_rescues_a_failed_first_extra(fake):
    fm = fake({PRIMARY: 300, CF: -1, TG: 80})
    assert measure_multi(1, PRIMARY, [CF, TG], 10, 1, 2) == 300
    assert [c[0] for c in fm.calls] == [PRIMARY, CF, TG]


def test_google_only_server_is_rejected(fake):
    fake({PRIMARY: 300, CF: -1, TG: -1})
    assert measure_multi(1, PRIMARY, [CF, TG], 10, 1, 2) == -1


def test_all_destinations_required_when_min_ok_is_three(fake):
    fake({PRIMARY: 300, CF: 50, TG: -1})
    assert measure_multi(1, PRIMARY, [CF, TG], 10, 1, 3) == -1


def test_min_ok_is_clamped_to_the_number_of_urls(fake):
    fm = fake({PRIMARY: 300, CF: 50})
    assert measure_multi(1, PRIMARY, [CF], 10, 1, 9) == 300
    assert [c[0] for c in fm.calls] == [PRIMARY, CF]


def test_no_extra_urls_is_exactly_the_single_url_check(fake):
    fm = fake({PRIMARY: 300})
    assert measure_multi(1, PRIMARY, [], 10, 1, 2) == 300
    assert measure_multi(1, PRIMARY, None, 10, 1, 2) == 300
    assert [c[0] for c in fm.calls] == [PRIMARY, PRIMARY]


def test_extras_use_a_short_timeout_and_no_retry(fake):
    fm = fake({PRIMARY: 300, CF: -1, TG: 50})
    measure_multi(1, PRIMARY, [CF, TG], 10, 3, 2)
    assert fm.calls[0] == (PRIMARY, 10, 3)
    for _url, timeout, retries in fm.calls[1:]:
        assert timeout == tester.EXTRA_URL_TIMEOUT_SECONDS
        assert retries == 0
    # a primary timeout already below the extras' cap is respected
    fm.calls.clear()
    measure_multi(1, PRIMARY, [CF, TG], 4, 0, 2)
    assert fm.calls[1][1] == 4


def _tester_settings(**test_overrides):
    test = {"url": PRIMARY, "timeout_seconds": 1, "concurrency": 2, "retries": 0,
            "base_port": 24000, "chunk_size": 96}
    test.update(test_overrides)
    return {"paths": {"sing_box_bin": "/bin/true"}, "test": test,
            "dns": {"local_server": "192.168.1.1"}}


def test_group_tester_reads_extra_urls_for_both_pools():
    settings = _tester_settings(extra_urls=[CF, TG], min_destinations_ok=2)
    for pool in ("a", "b"):
        gt = GroupTester(settings, pool=pool)
        assert gt.extra_urls == [CF, TG]
        assert gt.min_destinations_ok == 2


def test_group_tester_without_extra_urls_keeps_single_url_behaviour():
    gt = GroupTester(_tester_settings())
    assert gt.extra_urls == []


def test_group_tester_chunk_uses_measure_multi(monkeypatch):
    settings = _tester_settings(extra_urls=[CF], min_destinations_ok=2)
    gt = GroupTester(settings)
    seen = []

    def fake_multi(port, url, extras, timeout, retries, min_ok):
        seen.append((url, tuple(extras), min_ok))
        return 123

    class FakeProc:
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            pass

    monkeypatch.setattr(tester, "measure_multi", fake_multi)
    monkeypatch.setattr(tester, "_port_open", lambda port: False)
    monkeypatch.setattr(tester, "_wait_ports", lambda ports, timeout: True)
    monkeypatch.setattr(tester.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(gt, "_check_items", lambda items: (True, ""))
    item = {"id": 7, "outbound": {"type": "direct"}}

    assert gt._run_chunk([item]) == {7: 123}
    assert seen == [(PRIMARY, (CF,), 2)]


# --- StandbyProbe ---------------------------------------------------------

def _session(by_url):
    """requests-like session: the probed destination is the `url=` query value."""
    from urllib.parse import parse_qs, urlparse
    session = Mock()

    def get(endpoint, headers=None, timeout=None):
        url = parse_qs(urlparse(endpoint).query)["url"][0]
        outcome = by_url[url]
        if outcome is None:
            raise requests.ConnectionError("no route")
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"delay": outcome}
        return response

    session.get.side_effect = get
    return session


def test_probe_ready_when_a_majority_of_destinations_answer():
    probe = StandbyProbe("127.0.0.1:9090", session=_session({PRIMARY: 100, CF: None, TG: 150}),
                         urls=[PRIMARY, CF, TG], min_ok=2)
    result = probe.probe("cfg-2")
    assert result.delay_ms == 100          # primary destination's latency
    assert result.destinations == {PRIMARY: 100, CF: None, TG: 150}
    assert probe.ready(["cfg-2"])
    assert CF in (result.last_error or "")  # partial failure stays visible
    assert probe.snapshot()["cfg-2"]["destinations"][CF] is None


def test_probe_not_ready_when_only_the_primary_answers():
    probe = StandbyProbe("127.0.0.1:9090", session=_session({PRIMARY: 100, CF: None, TG: None}),
                         urls=[PRIMARY, CF, TG], min_ok=2)
    result = probe.probe("cfg-2")
    assert result.delay_ms is None
    assert probe.ready(["cfg-2"]) == []
    assert not probe.snapshot()["cfg-2"]["ready"]


def test_probe_uses_the_fastest_answer_when_primary_is_down_but_majority_up():
    probe = StandbyProbe("127.0.0.1:9090", session=_session({PRIMARY: None, CF: 90, TG: 70}),
                         urls=[PRIMARY, CF, TG], min_ok=2)
    assert probe.probe("cfg-2").delay_ms == 70


def test_probe_recovers_after_a_failed_round():
    answers = {PRIMARY: 100, CF: None, TG: None}
    probe = StandbyProbe("127.0.0.1:9090", session=_session(answers),
                         urls=[PRIMARY, CF, TG], min_ok=2)
    probe.probe("cfg-2")
    assert probe.ready(["cfg-2"]) == []
    answers[CF] = 60
    probe.probe("cfg-2")
    assert [r.tag for r in probe.ready(["cfg-2"])] == ["cfg-2"]


def test_probe_with_explicit_url_checks_only_that_destination():
    session = _session({PRIMARY: 100, CF: None, TG: None})
    probe = StandbyProbe("127.0.0.1:9090", session=session,
                         urls=[PRIMARY, CF, TG], min_ok=2)
    result = probe.probe("cfg-2", PRIMARY)
    assert result.delay_ms == 100
    assert session.get.call_count == 1


def test_probe_min_ok_is_clamped_and_defaults_to_single_url():
    probe = StandbyProbe("127.0.0.1:9090", session=_session({PRIMARY: 10}),
                         urls=[PRIMARY], min_ok=5)
    assert probe.min_ok == 1
    default = StandbyProbe("127.0.0.1:9090")
    assert len(default.urls) == 1


# --- settings / wiring ----------------------------------------------------

def test_default_settings_enable_multi_destination_checks(tmp_path):
    cfg = tmp_path / "config.yaml"
    # an installed config that predates the new keys
    cfg.write_text('test:\n  url: "https://www.gstatic.com/generate_204"\n  timeout_seconds: 10\n')
    settings = Settings(str(cfg))
    assert settings["test"]["extra_urls"] == [
        "https://cp.cloudflare.com/generate_204", "https://web.telegram.org/"]
    assert settings["test"]["min_destinations_ok"] == 2


def test_empty_extra_urls_in_config_restores_single_url_check(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("test:\n  extra_urls: []\n")
    assert Settings(str(cfg))["test"]["extra_urls"] == []


def test_create_monitor_gives_the_probe_all_destinations(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{}\n")
    settings = Settings(str(cfg))
    monitor = create_monitor(settings, Mock())
    assert monitor._probe.urls == [
        "https://www.gstatic.com/generate_204",
        "https://cp.cloudflare.com/generate_204",
        "https://web.telegram.org/",
    ]
    assert monitor._probe.min_ok == 2
