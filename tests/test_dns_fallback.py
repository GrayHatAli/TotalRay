"""Tests for the dnsmasq multi-upstream DNS fallback wiring.

Covers `_dnsmasq_uid()` and the resulting `exclude_uid` on the TUN
inbound in `build_config`, which lets dnsmasq's own fallback queries
(see dnsmasq/pi-gateway.conf) bypass sing-box's auto_redirect instead
of looping back into sing-box's own (possibly unhealthy) DNS pipeline.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from totalray.builder import _dnsmasq_uid, build_config


def _settings(tmp_path):
    settings = MagicMock()
    settings.__getitem__ = lambda self, key: {
        "routing": {"iran_direct": False, "block_ads": False,
                    "block_quic": False, "custom_rules": []},
        "tun": {"interface": "singtun0", "stack": "mixed", "mtu": 1500},
        "dns": {"remote_server": "1.1.1.1", "local_server": "192.168.1.1",
                "prefer_ipv4": True},
        "proxy_group": {"urltest_interval": "3m", "urltest_tolerance": 50,
                        "idle_timeout": "30m"},
        "clash_api": {"listen": "127.0.0.1:9090", "secret": ""},
        "local_proxy": {"port": 2080},
        "lan_proxy": {"enabled": False},
        "paths": {"rules_dir": str(tmp_path / "rules"),
                  "sing_box_data_dir": str(tmp_path / "data")},
    }[key]
    settings.rules_dir = str(tmp_path / "rules")
    os.makedirs(settings.rules_dir, exist_ok=True)
    return settings


class TestDnsmasqUid:
    """_dnsmasq_uid() looks up the dnsmasq system user by name."""

    @patch("totalray.builder.pwd.getpwnam")
    def test_returns_uid_when_user_exists(self, mock_getpwnam):
        mock_getpwnam.return_value = MagicMock(pw_uid=988)
        assert _dnsmasq_uid() == 988
        mock_getpwnam.assert_called_once_with("dnsmasq")

    @patch("totalray.builder.pwd.getpwnam")
    def test_returns_none_when_user_missing(self, mock_getpwnam):
        mock_getpwnam.side_effect = KeyError("dnsmasq")
        assert _dnsmasq_uid() is None


class TestBuildConfigExcludeUid:
    """build_config wires _dnsmasq_uid() into the TUN inbound."""

    @patch("totalray.builder._dnsmasq_uid")
    def test_excludes_dnsmasq_uid_when_found(self, mock_uid, tmp_path):
        mock_uid.return_value = 988
        config = build_config(_settings(tmp_path), [])
        tun_in = config["inbounds"][0]
        assert tun_in["type"] == "tun"
        assert tun_in["exclude_uid"] == [988]

    @patch("totalray.builder._dnsmasq_uid")
    def test_no_exclude_uid_when_user_not_found(self, mock_uid, tmp_path):
        mock_uid.return_value = None
        config = build_config(_settings(tmp_path), [])
        tun_in = config["inbounds"][0]
        assert tun_in["type"] == "tun"
        assert "exclude_uid" not in tun_in
