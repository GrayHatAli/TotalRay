"""Guards for the shell scripts that run as root on the gateway."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


@pytest.mark.parametrize("name", sorted(p.name for p in SCRIPTS.glob("*.sh")))
def test_script_has_valid_bash_syntax(name):
    result = subprocess.run(["bash", "-n", str(SCRIPTS / name)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_update_builds_the_core_before_stopping_the_gateway():
    text = (SCRIPTS / "update.sh").read_text()
    build = text.index("make -C")
    first_stop = text.index("systemctl stop")
    assert build < first_stop, "compiling must not happen while the LAN is offline"


def test_install_does_not_pipe_urandom_into_head():
    """Under `set -o pipefail` that pipeline exits 141 and aborts the install."""
    text = (SCRIPTS / "install.sh").read_text()
    assert "pipefail" in text
    code = "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("#"))
    assert not re.search(r"/dev/urandom\s*\|", code)


def test_install_does_not_pull_the_too_old_distro_go():
    text = (SCRIPTS / "install.sh").read_text()
    assert "golang-go" not in text
    assert "TOTALRAY_PREBUILT_CORE" in text


def test_uninstall_removes_the_files_install_creates():
    install = (SCRIPTS / "install.sh").read_text()
    uninstall = (SCRIPTS / "uninstall.sh").read_text()
    for path in ("/etc/sysctl.d/99-totalray.conf",
                 "/etc/systemd/system/sing-box.service",
                 "/usr/local/sbin/totalray-update",
                 "/usr/local/libexec/totalray"):
        assert path in uninstall, path
    assert "99-pivpn" not in uninstall
    assert "99-totalray.conf" in install


class TestNetemFailoverScript:
    """The tc commands of the Phase 6 failover test (nothing here needs root)."""

    SCRIPT = str(SCRIPTS / "netem_failover_test.sh")

    def _plan(self, *args):
        result = subprocess.run(["bash", self.SCRIPT, "plan", *args],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return result.stdout.splitlines()

    def test_only_the_named_server_is_impaired(self):
        lines = self._plan("eth0", "93.184.216.34")
        # unmatched traffic is pinned to band 2; netem sits only on band 3
        assert lines[0].endswith("priomap " + " ".join(["1"] * 16))
        assert lines[1] == ("tc qdisc replace dev eth0 parent 1:3 handle 30: "
                            "netem loss 25% delay 150ms 50ms")
        filters = [line for line in lines if " filter " in line]
        assert filters == [
            "tc filter add dev eth0 protocol ip parent 1: prio 1 u32 "
            "match ip dst 93.184.216.34/32 flowid 1:3"]

    def test_ipv4_and_ipv6_addresses_each_get_their_own_filter(self):
        lines = self._plan("eth0", "93.184.216.34", "2606:4700::1111")
        filters = [line for line in lines if " filter " in line]
        assert len(filters) == 2
        assert "protocol ip " in filters[0] and "match ip dst 93.184.216.34/32" in filters[0]
        assert "protocol ipv6 " in filters[1] and "match ip6 dst 2606:4700::1111/128" in filters[1]
        assert all(line.endswith("flowid 1:3") for line in filters)

    def test_profile_can_be_overridden(self):
        result = subprocess.run(
            ["bash", self.SCRIPT, "plan", "wlan0", "1.2.3.4"],
            capture_output=True, text=True, env={"NETEM_PROFILE": "loss 50%", "PATH": "/usr/bin:/bin"})
        assert "netem loss 50%" in result.stdout

    def test_help_and_bad_commands_need_no_root(self):
        helped = subprocess.run(["bash", self.SCRIPT, "help"], capture_output=True, text=True)
        assert helped.returncode == 0 and "netem" in helped.stdout
        assert "set -euo pipefail" not in helped.stdout
        bad = subprocess.run(["bash", self.SCRIPT, "nope"], capture_output=True, text=True)
        assert bad.returncode == 2 and "unknown command" in bad.stderr
