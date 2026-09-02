"""Tests for builder.restart_singbox()'s crash-loop recovery.

Covers the known upstream sing-box bug where repeated `systemctl restart`
calls can leave a stale loopback route behind, causing the next startup
to fail immediately with "append ipv4 loopback route: file exists".
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

from totalray import builder


def _proc(returncode=0, stdout="", stderr=""):
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


@patch("totalray.builder.time.sleep")
@patch("totalray.builder.subprocess.run")
def test_clean_start_no_crash(mock_run, mock_sleep):
    """Normal path: stop succeeds, the proactive stale-route sweep finds
    nothing to clear, start succeeds on first try."""
    mock_run.side_effect = [
        _proc(0),             # stop
        _proc(0, stdout=""),  # proactive: ip route show table main (clean)
        _proc(0),             # start
    ]
    ok, msg = builder.restart_singbox()
    assert ok is True
    assert msg == "restarted"
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert calls[0][:2] == ["systemctl", "stop"]
    assert calls[1][:4] == ["ip", "route", "show", "table"]
    assert calls[2][:2] == ["systemctl", "start"]


@patch("totalray.builder.time.sleep")
@patch("totalray.builder.subprocess.run")
def test_recovers_from_stale_route_crash_loop(mock_run, mock_sleep):
    """Proactive sweep finds nothing; start fails with the known
    stale-route signature; journalctl confirms it; the reactive sweep
    then finds and clears the route, and the retry succeeds."""
    mock_run.side_effect = [
        _proc(0),                                     # stop
        _proc(0, stdout=""),                          # proactive sweep (clean)
        _proc(1, stderr="FATAL: ... append ipv4 loopback route: "
                         "file exists"),               # start (fails)
        _proc(0, stdout="... append ipv4 loopback route: file exists"),
        _proc(0, stdout="local 127.0.0.1 dev eth0 scope host"),  # reactive sweep (finds it)
        _proc(0),                                     # ip route del
        _proc(0),                                     # start (retry, ok)
    ]
    ok, msg = builder.restart_singbox()
    assert ok is True
    assert "recovered from stale-route crash-loop" in msg
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert calls[0][:2] == ["systemctl", "stop"]
    assert calls[1][:4] == ["ip", "route", "show", "table"]
    assert calls[2][:2] == ["systemctl", "start"]
    assert calls[3][:1] == ["journalctl"]
    assert calls[4][:4] == ["ip", "route", "show", "table"]
    assert calls[5][:3] == ["ip", "route", "del"]
    assert calls[6][:2] == ["systemctl", "start"]


@patch("totalray.builder.time.sleep")
@patch("totalray.builder.subprocess.run")
def test_unrelated_start_failure_not_retried(mock_run, mock_sleep):
    """A start failure that is NOT the known signature should be
    returned as-is, with no reactive route-clearing retry attempted."""
    mock_run.side_effect = [
        _proc(0),                                     # stop
        _proc(0, stdout=""),                          # proactive sweep (clean)
        _proc(1, stderr="some other fatal error"),    # start (fails)
        _proc(0, stdout="some other fatal error"),     # journalctl
    ]
    ok, msg = builder.restart_singbox()
    assert ok is False
    assert "some other fatal error" in msg
    assert mock_run.call_count == 4  # no reactive sweep, no retry start


@patch("totalray.builder.time.sleep")
@patch("totalray.builder.subprocess.run")
def test_stop_failure_short_circuits(mock_run, mock_sleep):
    """If stop itself raises, we should not attempt to start at all."""
    mock_run.side_effect = OSError("systemctl not found")
    ok, msg = builder.restart_singbox()
    assert ok is False
    assert "stop failed" in msg
    assert mock_run.call_count == 1
