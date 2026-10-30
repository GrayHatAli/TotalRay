"""RetransmitSampler: report growth of retransmits on the tunnel's sockets.

The kernel keeps a lifetime total per socket, so a busy gateway always has
big totals. Only growth between samples says anything about the link.
"""
from __future__ import annotations

from unittest.mock import Mock

from totalray.health_signal import DegradationDetector, RetransmitSampler

HEADER = "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process"


def _ss(*sockets) -> Mock:
    """sockets: (local, peer, process_name_or_None, lifetime_retrans_segments)"""
    lines = [HEADER]
    for local, peer, process, total in sockets:
        users = f' users:(("{process}",pid=812,fd=15))' if process else ""
        lines.append(f"ESTAB 0 0 {local} {peer}{users}")
        counters = "\t cubic wscale:7,7 rto:204 rtt:3/1 bytes_sent:90000 segs_out:600"
        if total:
            counters += f" bytes_retrans:{total * 1448} retrans:0/{total}"
        lines.append(counters)
    return Mock(return_value=Mock(stdout="\n".join(lines) + "\n"))


def _sampler(*snapshots):
    """A sampler whose successive ss calls return the given outputs."""
    runner = Mock(side_effect=[s.return_value for s in snapshots])
    return RetransmitSampler(runner)


TUNNEL = ("192.168.1.250:40000", "8.8.8.8:443", "singbox-core")


def test_first_sample_is_only_a_baseline():
    sampler = _sampler(_ss((*TUNNEL[:3], 500)))
    assert sampler.sample() == 0


def test_returns_growth_not_the_lifetime_total():
    sampler = _sampler(
        _ss((*TUNNEL[:3], 5)),
        _ss((*TUNNEL[:3], 9)),
        _ss((*TUNNEL[:3], 9)),
    )
    assert sampler.sample() == 0
    assert sampler.sample() == 4
    assert sampler.sample() == 0


def test_steady_old_retransmits_never_degrade_the_detector():
    """A socket with a large lifetime total must not look like trouble."""
    sampler = _sampler(*[_ss((*TUNNEL[:3], 800)) for _ in range(6)])
    detector = DegradationDetector(retransmit_threshold=3, hysteresis_samples=3)
    for i in range(6):
        assert not detector.observe_retransmits(sampler.sample(), now=5.0 * i)
    assert not detector.snapshot(now=30.0)["degraded"]


def test_new_socket_after_baseline_counts_its_retransmits():
    sampler = _sampler(
        _ss((*TUNNEL, 0)),
        _ss((*TUNNEL, 0), ("192.168.1.250:40001", "1.1.1.1:443", "sing-box", 4)),
    )
    sampler.sample()
    assert sampler.sample() == 4


def test_closed_socket_does_not_count_or_go_negative():
    sampler = _sampler(
        _ss((*TUNNEL[:3], 50)),
        _ss(("192.168.1.250:40009", "9.9.9.9:443", "singbox-core", 0)),
    )
    sampler.sample()
    assert sampler.sample() == 0


def test_ignores_sockets_that_are_not_the_tunnel():
    lan_client = ("192.168.1.250:2081", "192.168.1.20:51000", "singbox-core", 0)
    loopback = ("127.0.0.1:9090", "127.0.0.1:41000", "singbox-core", 0)
    other_process = ("192.168.1.250:50000", "8.8.4.4:443", "curl", 0)
    unknown_owner = ("192.168.1.250:50001", "8.8.4.4:80", None, 0)
    grown = [("192.168.1.250:2081", "192.168.1.20:51000", "singbox-core", 40),
             ("127.0.0.1:9090", "127.0.0.1:41000", "singbox-core", 40),
             ("192.168.1.250:50000", "8.8.4.4:443", "curl", 40),
             ("192.168.1.250:50001", "8.8.4.4:80", None, 40)]
    sampler = _sampler(_ss(lan_client, loopback, other_process, unknown_owner),
                       _ss(*grown))
    sampler.sample()
    assert sampler.sample() == 0


def test_public_ipv6_peer_counts():
    local = "[fd00::250]:40000"
    peer = "[2606:4700::1111]:443"
    sampler = _sampler(_ss((local, peer, "singbox-core", 1)),
                       _ss((local, peer, "singbox-core", 6)))
    sampler.sample()
    assert sampler.sample() == 5


def test_runner_failure_returns_zero():
    sampler = RetransmitSampler(Mock(side_effect=OSError("no ss")))
    assert sampler.sample() == 0
