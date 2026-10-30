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
