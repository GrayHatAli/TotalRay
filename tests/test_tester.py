"""GroupTester port handling.

Pool A and Pool B rounds run at the same time and each test sing-box binds
one local port per config. These tests pin down that the two pools never
share a port, and that a port clash fails loudly instead of measuring the
wrong sing-box.
"""
from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import pytest

from totalray import tester
from totalray.tester import GroupTester

ITEM = {"id": 1, "outbound": {"type": "direct"}}


def _settings(base_port=24000, chunk_size=96):
    return {
        "paths": {"sing_box_bin": "/bin/true"},
        "test": {"url": "http://example.invalid", "timeout_seconds": 1,
                 "concurrency": 2, "retries": 0, "base_port": base_port,
                 "chunk_size": chunk_size},
        "dns": {"local_server": "192.168.1.1"},
    }


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_pool_a_and_pool_b_use_separate_port_ranges():
    settings = _settings(base_port=24000, chunk_size=96)
    a = GroupTester(settings, pool="a")
    b = GroupTester(settings, pool="b")
    a_ports = set(range(a.base_port, a.base_port + a.chunk_size))
    b_ports = set(range(b.base_port, b.base_port + b.chunk_size))
    assert a.base_port == 24000
    assert not (a_ports & b_ports)


def test_default_pool_is_a():
    settings = _settings()
    assert GroupTester(settings).base_port == GroupTester(settings, pool="a").base_port


def test_busy_test_port_raises_before_starting_sing_box():
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        busy_port = srv.getsockname()[1]
        gt = GroupTester(_settings(base_port=busy_port, chunk_size=4))
        with patch.object(gt, "_check_items", return_value=(True, "")), \
             patch.object(tester.subprocess, "Popen") as popen:
            with pytest.raises(RuntimeError, match="already in use"):
                gt._run_chunk([ITEM])
        popen.assert_not_called()


def test_sing_box_exiting_during_startup_raises():
    gt = GroupTester(_settings(base_port=_free_port(), chunk_size=4))
    proc = MagicMock()
    proc.poll.return_value = 1
    proc.returncode = 1
    with patch.object(gt, "_check_items", return_value=(True, "")), \
         patch.object(tester, "_wait_ports", return_value=True), \
         patch.object(tester.subprocess, "Popen", return_value=proc):
        with pytest.raises(RuntimeError, match="exited during startup"):
            gt._run_chunk([ITEM])
    proc.terminate.assert_called()
