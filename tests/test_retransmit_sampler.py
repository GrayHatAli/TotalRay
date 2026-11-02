"""Retransmit signal: measured on the active server's sockets, as a share of
what was sent, over a window.

The kernel keeps lifetime totals per socket and a busy link always
retransmits some segments, so neither a lifetime total nor a small absolute
count says anything about the link.
"""
from __future__ import annotations

import json
from unittest.mock import Mock, patch

from totalray.health_signal import DegradationDetector, RetransmitSample, RetransmitSampler
from totalray.live_monitor import LiveMonitor
from totalray.standby_probe import StandbyProbe

HEADER = "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process"


def _ss(*sockets) -> Mock:
    """sockets: (local, peer, process_or_None, lifetime_retrans_segments, lifetime_segs_out)"""
    lines = [HEADER]
    for local, peer, process, retrans, segs in sockets:
        users = f' users:(("{process}",pid=812,fd=15))' if process else ""
        lines.append(f"ESTAB 0 0 {local} {peer}{users}")
        counters = f"\t cubic wscale:7,7 rto:204 rtt:3/1 bytes_sent:90000 segs_out:{segs}"
        if retrans:
            counters += f" bytes_retrans:{retrans * 1448} retrans:0/{retrans}"
        lines.append(counters)
    return Mock(return_value=Mock(stdout="\n".join(lines) + "\n"))


def _sampler(*snapshots):
    """A sampler whose successive ss calls return the given outputs."""
    return RetransmitSampler(Mock(side_effect=[s.return_value for s in snapshots]))


def _tunnel(retrans, segs, peer="8.8.8.8:443", local="192.168.1.250:40000"):
    return (local, peer, "singbox-core", retrans, segs)


# ---- sampler ---------------------------------------------------------------

def test_first_sample_is_only_a_baseline():
    sampler = _sampler(_ss(_tunnel(500, 90000)))
    assert sampler.sample() == RetransmitSample(0, 0)


def test_returns_growth_not_lifetime_totals():
    sampler = _sampler(_ss(_tunnel(5, 100)), _ss(_tunnel(9, 160)), _ss(_tunnel(9, 160)))
    assert sampler.sample() == (0, 0)
    assert sampler.sample() == (4, 60)
    assert sampler.sample() == (0, 0)


def test_new_socket_after_baseline_counts_its_whole_life():
    sampler = _sampler(
        _ss(_tunnel(0, 10)),
        _ss(_tunnel(0, 10), _tunnel(4, 40, local="192.168.1.250:40001", peer="1.1.1.1:443")))
    sampler.sample()
    assert sampler.sample() == (4, 40)


def test_closed_socket_does_not_count_or_go_negative():
    sampler = _sampler(_ss(_tunnel(50, 1000)),
                       _ss(_tunnel(0, 5, local="192.168.1.250:40009", peer="9.9.9.9:443")))
    sampler.sample()
    assert sampler.sample() == (0, 5)


def test_ignores_sockets_that_are_not_the_tunnel():
    sockets = [("192.168.1.250:2081", "192.168.1.20:51000", "singbox-core"),   # LAN client
               ("127.0.0.1:9090", "127.0.0.1:41000", "singbox-core"),          # loopback
               ("192.168.1.250:50000", "8.8.4.4:443", "curl"),                 # other process
               ("192.168.1.250:50001", "8.8.4.4:80", None)]                    # owner unknown
    sampler = _sampler(_ss(*[(*s, 0, 10) for s in sockets]),
                       _ss(*[(*s, 40, 500) for s in sockets]))
    sampler.sample()
    assert sampler.sample() == (0, 0)


def test_public_ipv6_peer_counts():
    local, peer = "[fd00::250]:40000", "[2606:4700::1111]:443"
    sampler = _sampler(_ss((local, peer, "singbox-core", 1, 100)),
                       _ss((local, peer, "singbox-core", 6, 400)))
    sampler.sample()
    assert sampler.sample() == (5, 300)


def test_runner_failure_returns_zero():
    assert RetransmitSampler(Mock(side_effect=OSError("no ss"))).sample() == (0, 0)


def test_only_the_active_servers_sockets_are_measured():
    active = _tunnel(0, 100, peer="8.8.8.8:443")
    other = _tunnel(0, 100, peer="1.1.1.1:443", local="192.168.1.250:40001")
    sampler = _sampler(
        _ss(active, other),
        _ss(_tunnel(10, 400, peer="8.8.8.8:443"),
            _tunnel(90, 900, peer="1.1.1.1:443", local="192.168.1.250:40001")))
    sampler.sample({"8.8.8.8"})
    assert sampler.sample({"8.8.8.8"}) == (10, 300)   # the other server's 90 are not ours


def test_changing_the_filter_does_not_dump_a_sockets_history():
    """After a switch the new server's old sockets must not show up as a spike."""
    a = _tunnel(50, 1000, peer="8.8.8.8:443")
    b = _tunnel(70, 2000, peer="1.1.1.1:443", local="192.168.1.250:40001")
    sampler = _sampler(_ss(a, b), _ss(a, b))
    sampler.sample({"8.8.8.8"})                       # active = A
    assert sampler.sample({"1.1.1.1"}) == (0, 0)      # switched to B, nothing new happened


def test_ipv6_peers_compare_in_canonical_form():
    local, peer = "[fd00::250]:40000", "[2606:4700::1111]:443"
    sampler = _sampler(_ss((local, peer, "singbox-core", 0, 10)),
                       _ss((local, peer, "singbox-core", 3, 110)))
    sampler.sample({"2606:4700:0:0:0:0:0:1111"})
    assert sampler.sample({"2606:4700:0:0:0:0:0:1111"}) == (3, 100)


# ---- detector ----------------------------------------------------------------

def _feed(detector, samples, step=5.0):
    result = False
    for i, (retrans, segs) in enumerate(samples):
        result = detector.observe_retransmits(retrans, segs, now=step * i)
    return result


def test_real_link_noise_does_not_degrade():
    """Per-5s (retransmits, segments) measured on the live gateway, healthy but lossy."""
    measured = [(4, 188), (0, 23), (7, 167), (10, 123), (4, 34), (4, 37), (1, 60),
                (0, 107), (0, 163), (0, 78), (2, 49), (0, 77), (0, 33), (0, 18)]
    detector = DegradationDetector()
    for i, (retrans, segs) in enumerate(measured):
        assert not detector.observe_retransmits(retrans, segs, now=5.0 * i), i


def test_netem_loss_on_the_real_link_degrades_and_the_healthy_baseline_does_not():
    """Per-5s (retransmits, segments) on the active server: healthy, then 25% netem loss
    (measured on the live gateway; throughput collapses under loss, so segment counts drop)."""
    healthy = [(4, 189), (1, 233), (5, 385)]
    under_loss = [(17, 157), (13, 60), (14, 77), (8, 39), (12, 59), (14, 52), (1, 26), (9, 52), (14, 110)]
    detector = DegradationDetector()
    for i, (retrans, segs) in enumerate(healthy):
        assert not detector.observe_retransmits(retrans, segs, now=5.0 * i)
    flags = [detector.observe_retransmits(r, g, now=5.0 * (len(healthy) + i))
             for i, (r, g) in enumerate(under_loss)]
    assert any(flags), flags
    # the 10s window still holds healthy samples at first, then three bad
    # evaluations are needed (hysteresis): about 25s after the loss starts
    assert flags.index(True) <= 5, "should notice within about 30s of the loss starting"


def test_heavy_loss_degrades():
    detector = DegradationDetector()
    assert _feed(detector, [(30, 120), (35, 140), (30, 110), (28, 100)])


def test_busy_link_with_a_tiny_loss_rate_is_fine():
    detector = DegradationDetector()
    assert not _feed(detector, [(27, 5000)] * 6)       # 0.5%


def test_a_burst_with_too_few_segments_is_ignored():
    detector = DegradationDetector()
    assert not _feed(detector, [(4, 8)] * 6)           # 50% of almost nothing


def test_snapshot_reports_samples_and_ratio():
    detector = DegradationDetector()
    detector.observe_retransmits(6, 100, now=0.0)
    detector.observe_retransmits(4, 100, now=5.0)
    snap = detector.snapshot(now=6.0)
    assert snap["retransmit_samples"] == [[6, 100], [4, 100]]
    assert snap["retransmit_ratio"] == 0.05


# ---- standby probe table / server lookup -----------------------------------------

def test_probe_forgets_servers_that_left_the_group():
    probe = StandbyProbe("127.0.0.1:9090", session=Mock())
    probe._results.update({"cfg-1": Mock(), "cfg-2": Mock(), "cfg-3": Mock()})
    probe.keep_only(["cfg-2", "cfg-9"])
    assert set(probe._results) == {"cfg-2"}


def _monitor(tmp_path, outbounds):
    path = tmp_path / "sing-box.json"
    path.write_text(json.dumps({"outbounds": outbounds}))
    settings = Mock(data_dir=str(tmp_path))
    settings.data = {"clash_api": {"listen": "127.0.0.1:9090", "secret": ""},
                     "paths": {"sing_box_config": str(path)}}
    return LiveMonitor(settings, Mock())


def test_server_ips_from_a_literal_address(tmp_path):
    monitor = _monitor(tmp_path, [{"tag": "cfg-1", "server": "93.184.216.34"}])
    assert monitor._active_server_ips("cfg-1") == {"93.184.216.34"}


def test_server_ips_resolve_a_hostname_once_and_cache_it(tmp_path):
    monitor = _monitor(tmp_path, [{"tag": "cfg-1", "server": "proxy.example"}])
    infos = [(2, 1, 6, "", ("93.184.216.34", 0)), (10, 1, 6, "", ("2606:4700::1111", 0, 0, 0))]
    with patch("totalray.live_monitor.socket.getaddrinfo", return_value=infos) as lookup:
        assert monitor._active_server_ips("cfg-1") == {"93.184.216.34", "2606:4700::1111"}
        assert monitor._active_server_ips("cfg-1") == {"93.184.216.34", "2606:4700::1111"}
    assert lookup.call_count == 1


def test_unknown_server_means_unfiltered(tmp_path):
    monitor = _monitor(tmp_path, [{"tag": "cfg-1", "server": "93.184.216.34"}])
    assert monitor._active_server_ips("cfg-404") is None


def test_only_live_group_members_are_probed(tmp_path):
    monitor = _monitor(tmp_path, [])
    monitor._probe = Mock()
    monitor._probe_standbys("cfg-1", ["cfg-1", "cfg-2", "cfg-3"])
    monitor._probe.keep_only.assert_called_once_with(["cfg-1", "cfg-2", "cfg-3"])
    assert [c.args[0] for c in monitor._probe.probe.call_args_list] == ["cfg-2", "cfg-3"]
