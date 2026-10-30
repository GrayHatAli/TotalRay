"""bypass_tun patches process-wide socket functions, so overlapping uses
from two threads must not interleave."""
from __future__ import annotations

import socket
import threading

from totalray import net


def test_overlapping_bypass_tun_uses_are_serialised():
    a_inside = threading.Event()
    release_a = threading.Event()
    b_entered = threading.Event()
    seen_by_a_before_exit = []

    def job_a():
        with net.bypass_tun():
            a_inside.set()
            release_a.wait(5)
            seen_by_a_before_exit.append(socket.socket is net._MarkedSocket)

    def job_b():
        a_inside.wait(5)
        with net.bypass_tun():
            b_entered.set()

    ta = threading.Thread(target=job_a)
    tb = threading.Thread(target=job_b)
    ta.start()
    tb.start()
    assert a_inside.wait(5)
    # B must wait for A; it must not enter, nor restore sockets under A
    assert not b_entered.wait(0.3)
    release_a.set()
    ta.join(5)
    tb.join(5)

    assert seen_by_a_before_exit == [True]
    assert b_entered.is_set()
    assert socket.socket is net._real_socket
    assert socket.getaddrinfo is net._real_getaddrinfo


def test_nested_use_in_one_thread_does_not_deadlock():
    with net.bypass_tun():
        with net.bypass_tun():
            assert socket.socket is net._MarkedSocket
    assert socket.socket is net._real_socket
