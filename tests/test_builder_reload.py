"""Tests for builder.reload_singbox() -- the SIGHUP-based reload path
that replaces a full stop/start cycle for routine config changes
(e.g. Pool B churn), falling back to restart_singbox() whenever the
reload can't be confirmed.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

from totalray import builder


SETTINGS = {"clash_api": {"listen": "127.0.0.1:9090", "secret": "s3cr3t"}}


def _proc(returncode=0, stdout="", stderr=""):
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


# --- _singbox_is_active -----------------------------------------------

@patch("totalray.builder.subprocess.run")
def test_is_active_true_on_zero_exit(mock_run):
    mock_run.return_value = _proc(0)
    assert builder._singbox_is_active() is True
    calls = mock_run.call_args_list
    assert calls[0].args[0] == ["systemctl", "is-active", "--quiet", "sing-box"]


@patch("totalray.builder.subprocess.run")
def test_is_active_false_on_nonzero_exit(mock_run):
    mock_run.return_value = _proc(3)
    assert builder._singbox_is_active() is False


@patch("totalray.builder.subprocess.run")
def test_is_active_false_when_systemctl_missing(mock_run):
    mock_run.side_effect = OSError("systemctl not found")
    assert builder._singbox_is_active() is False


# --- _clash_api_alive ---------------------------------------------------

@patch("totalray.builder.requests.get")
def test_clash_api_alive_true_on_200(mock_get):
    mock_get.return_value = MagicMock(status_code=200)
    assert builder._clash_api_alive(SETTINGS) is True
    args, kwargs = mock_get.call_args
    assert args[0] == "http://127.0.0.1:9090/version"
    assert kwargs["headers"] == {"Authorization": "Bearer s3cr3t"}


@patch("totalray.builder.requests.get")
def test_clash_api_alive_false_on_non_200(mock_get):
    mock_get.return_value = MagicMock(status_code=500)
    assert builder._clash_api_alive(SETTINGS) is False


@patch("totalray.builder.requests.get")
def test_clash_api_alive_false_on_connection_error(mock_get):
    import requests
    mock_get.side_effect = requests.ConnectionError("refused")
    assert builder._clash_api_alive(SETTINGS) is False


# --- reload_singbox -------------------------------------------------------

@patch("totalray.builder.time.sleep")
@patch("totalray.builder.restart_singbox")
@patch("totalray.builder._clash_api_alive")
@patch("totalray.builder._singbox_is_active")
@patch("totalray.builder.subprocess.run")
def test_reload_success_via_sighup(mock_run, mock_active, mock_alive,
                                    mock_restart, mock_sleep):
    """Service is running; HUP is sent; the new instance confirms
    itself alive on the first poll. restart_singbox must never be
    called."""
    mock_run.return_value = _proc(0)
    mock_active.return_value = True
    mock_alive.return_value = True

    ok, msg = builder.reload_singbox(SETTINGS)

    assert ok is True
    assert msg == "reloaded via SIGHUP"
    kill_calls = [c.args[0] for c in mock_run.call_args_list]
    assert ["systemctl", "kill", "-s", "HUP", "sing-box"] in kill_calls
    mock_restart.assert_not_called()


@patch("totalray.builder.time.sleep")
@patch("totalray.builder.restart_singbox")
@patch("totalray.builder._clash_api_alive")
@patch("totalray.builder._singbox_is_active")
@patch("totalray.builder.subprocess.run")
def test_reload_falls_back_when_not_confirmed(mock_run, mock_active, mock_alive,
                                               mock_restart, mock_sleep):
    """HUP is sent but the Clash API never comes back up within the
    poll window -- must fall back to the proven stop/start path."""
    mock_run.return_value = _proc(0)
    mock_active.return_value = True
    mock_alive.return_value = False  # never confirms
    mock_restart.return_value = (True, "restarted")

    ok, msg = builder.reload_singbox(SETTINGS)

    assert ok is True
    assert msg == "restarted"
    mock_restart.assert_called_once()


@patch("totalray.builder.time.sleep")
@patch("totalray.builder.restart_singbox")
@patch("totalray.builder.subprocess.run")
def test_reload_falls_back_when_kill_signal_fails(mock_run, mock_restart, mock_sleep):
    """If we can't even send the signal (e.g. systemctl missing),
    fall back immediately without polling."""
    with patch("totalray.builder._singbox_is_active", return_value=True):
        mock_run.side_effect = OSError("systemctl not found")
        mock_restart.return_value = (False, "restart also failed")

        ok, msg = builder.reload_singbox(SETTINGS)

        assert ok is False
        assert msg == "restart also failed"
        mock_restart.assert_called_once()


@patch("totalray.builder._start_singbox")
@patch("totalray.builder._singbox_is_active")
def test_reload_starts_instead_when_not_running(mock_active, mock_start):
    """If sing-box isn't running at all, there's nothing to reload --
    go straight to a plain start, no SIGHUP, no restart_singbox."""
    mock_active.return_value = False
    mock_start.return_value = (True, "started")

    ok, msg = builder.reload_singbox(SETTINGS)

    assert ok is True
    assert msg == "started"
    mock_start.assert_called_once()
