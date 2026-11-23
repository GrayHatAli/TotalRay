"""Multi-destination health: a server must answer enough destinations.

The destinations are sites filtered in Iran (web.telegram.org, facebook.com):
a server that answers one of them can carry that traffic, whereas a generic
connectivity URL only proves the server is up. ``min_destinations_ok`` says how
many must answer: 1 = any one, N = all (see DNS_DETOUR_AND_MULTI_PROBE_PLAN.md).
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest
import requests

from totalray import tester
from totalray.live_monitor import create_monitor
from totalray.settings import Settings
from totalray.standby_probe import StandbyProbe
from totalray.tester import GroupTester, measure_multi

TG = "https://web.telegram.org/"
FB = "https://facebook.com/"
CF = "https://cloudflare.test/"


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


def urls_called(fm):
    return [c[0] for c in fm.calls]


# --- "any one is enough" (min_ok = 1): the production rule ----------------

def test_any_one_enough_first_url_answers_and_second_is_never_run(fake):
    fm = fake({TG: 321, FB: 50})
    assert measure_multi(1, TG, [FB], 10, 1, 1) == 321
    assert urls_called(fm) == [TG]


def test_any_one_enough_second_url_rescues_a_failed_first(fake):
    fm = fake({TG: -1, FB: 80})
    assert measure_multi(1, TG, [FB], 10, 1, 1) == 80   # the delay of the URL that answered
    assert urls_called(fm) == [TG, FB]


def test_any_one_enough_but_none_answers_is_unhealthy(fake):
    fm = fake({TG: -1, FB: -1})
    assert measure_multi(1, TG, [FB], 10, 1, 1) == -1
    assert urls_called(fm) == [TG, FB]


# --- stricter thresholds --------------------------------------------------

def test_both_required_rejects_after_the_first_failure(fake):
    fm = fake({TG: -1, FB: 50})
    assert measure_multi(1, TG, [FB], 10, 1, 2) == -1
    assert urls_called(fm) == [TG]       # FB can no longer rescue it: not run


def test_both_required_accepts_when_both_answer(fake):
    fake({TG: 300, FB: 50})
    assert measure_multi(1, TG, [FB], 10, 1, 2) == 300


def test_majority_of_three_without_the_first_url(fake):
    fm = fake({TG: -1, FB: 60, CF: 70})
    assert measure_multi(1, TG, [FB, CF], 10, 1, 2) == 60
    assert urls_called(fm) == [TG, FB, CF]


def test_majority_of_three_stops_as_soon_as_it_is_decided(fake):
    fm = fake({TG: 300, FB: 50, CF: 50})
    assert measure_multi(1, TG, [FB, CF], 10, 1, 2) == 300
    assert urls_called(fm) == [TG, FB]


def test_min_ok_is_clamped_to_the_number_of_urls(fake):
    fake({TG: 300, FB: 50})
    assert measure_multi(1, TG, [FB], 10, 1, 9) == 300
    fake({TG: 300, FB: -1})
    assert measure_multi(1, TG, [FB], 10, 1, 9) == -1


def test_min_ok_below_one_still_needs_one_answer(fake):
    fake({TG: -1, FB: -1})
    assert measure_multi(1, TG, [FB], 10, 1, 0) == -1


def test_no_extra_urls_is_exactly_the_single_url_check(fake):
    fm = fake({TG: 300})
    assert measure_multi(1, TG, [], 10, 1, 2) == 300
    assert measure_multi(1, TG, None, 10, 1, 2) == 300
    assert urls_called(fm) == [TG, TG]


def test_extras_use_a_short_timeout_and_no_retry(fake):
    fm = fake({TG: -1, FB: 50})
    measure_multi(1, TG, [FB], 10, 3, 1)
    assert fm.calls[0] == (TG, 10, 3)
    assert fm.calls[1] == (FB, tester.EXTRA_URL_TIMEOUT_SECONDS, 0)
    # a primary timeout already below the extras' cap is respected
    fm.calls.clear()
    measure_multi(1, TG, [FB], 4, 0, 1)
    assert fm.calls[1][1] == 4


# --- GroupTester wiring ---------------------------------------------------

def _tester_settings(**test_overrides):
    test = {"url": TG, "timeout_seconds": 1, "concurrency": 2, "retries": 0,
            "base_port": 24000, "chunk_size": 96}
    test.update(test_overrides)
    return {"paths": {"sing_box_bin": "/bin/true"}, "test": test,
            "dns": {"local_server": "192.168.1.1"}}


def test_group_tester_reads_extra_urls_for_both_pools():
    settings = _tester_settings(extra_urls=[FB], min_destinations_ok=1)
    for pool in ("a", "b"):
        gt = GroupTester(settings, pool=pool)
        assert gt.extra_urls == [FB]
        assert gt.min_destinations_ok == 1


def test_group_tester_without_extra_urls_keeps_single_url_behaviour():
    gt = GroupTester(_tester_settings())
    assert gt.extra_urls == []


def test_group_tester_chunk_uses_measure_multi(monkeypatch):
    settings = _tester_settings(extra_urls=[FB], min_destinations_ok=1)
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
    assert seen == [(TG, (FB,), 1)]


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


def test_probe_ready_when_any_one_destination_answers():
    probe = StandbyProbe("127.0.0.1:9090", session=_session({TG: None, FB: 150}),
                         urls=[TG, FB], min_ok=1)
    result = probe.probe("cfg-2")
    assert result.delay_ms == 150           # the destination that answered
    assert result.destinations == {TG: None, FB: 150}
    assert probe.ready(["cfg-2"])
    assert TG in (result.last_error or "")   # the partial failure stays visible
    assert probe.snapshot()["cfg-2"]["destinations"][TG] is None


def test_probe_not_ready_when_no_destination_answers():
    probe = StandbyProbe("127.0.0.1:9090", session=_session({TG: None, FB: None}),
                         urls=[TG, FB], min_ok=1)
    result = probe.probe("cfg-2")
    assert result.delay_ms is None
    assert probe.ready(["cfg-2"]) == []
    assert not probe.snapshot()["cfg-2"]["ready"]


def test_probe_reports_the_first_destinations_latency_when_it_answered():
    probe = StandbyProbe("127.0.0.1:9090", session=_session({TG: 100, FB: 40}),
                         urls=[TG, FB], min_ok=1)
    assert probe.probe("cfg-2").delay_ms == 100


def test_probe_with_both_required_needs_both():
    answers = {TG: 100, FB: None}
    probe = StandbyProbe("127.0.0.1:9090", session=_session(answers),
                         urls=[TG, FB], min_ok=2)
    probe.probe("cfg-2")
    assert probe.ready(["cfg-2"]) == []
    answers[FB] = 60
    probe.probe("cfg-2")
    assert [r.tag for r in probe.ready(["cfg-2"])] == ["cfg-2"]


def test_probe_with_explicit_url_checks_only_that_destination():
    session = _session({TG: 100, FB: None})
    probe = StandbyProbe("127.0.0.1:9090", session=session, urls=[TG, FB], min_ok=2)
    result = probe.probe("cfg-2", TG)
    assert result.delay_ms == 100
    assert session.get.call_count == 1


def test_probe_min_ok_is_clamped_and_defaults_to_single_url():
    probe = StandbyProbe("127.0.0.1:9090", session=_session({TG: 10}), urls=[TG], min_ok=5)
    assert probe.min_ok == 1
    assert len(StandbyProbe("127.0.0.1:9090").urls) == 1


# --- settings / wiring ----------------------------------------------------

def test_defaults_use_filtered_sites_and_any_one_is_enough(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{}\n")
    test = Settings(str(cfg))["test"]
    assert test["url"] == TG
    assert test["extra_urls"] == [FB]
    assert test["min_destinations_ok"] == 1


def test_config_without_the_new_keys_still_gets_the_extras(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text('test:\n  url: "https://web.telegram.org/"\n  timeout_seconds: 10\n')
    test = Settings(str(cfg))["test"]
    assert test["extra_urls"] == [FB]
    assert test["min_destinations_ok"] == 1


def test_empty_extra_urls_in_config_restores_single_url_check(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("test:\n  extra_urls: []\n")
    assert Settings(str(cfg))["test"]["extra_urls"] == []


def test_create_monitor_gives_the_probe_the_same_destinations(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{}\n")
    monitor = create_monitor(Settings(str(cfg)), Mock())
    assert monitor._probe.urls == [TG, FB]
    assert monitor._probe.min_ok == 1
