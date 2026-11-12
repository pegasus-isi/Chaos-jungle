"""System-command dependency installation during fault preflight."""

import pytest

from chaos_jungle.core.preflight import Preflight
from chaos_jungle.faults.base import PreflightError


class MissingCommandTarget:
    def __init__(self):
        self.installs = []
        self.probes = []

    def run(self, cmd):
        self.probes.append(cmd)
        if cmd == "which apt-get 2>/dev/null":
            return 0, "/usr/bin/apt-get", ""
        return 1, "", ""

    def sudo(self, cmd):
        self.installs.append(cmd)
        return 0, "", ""


@pytest.mark.parametrize(
    ("dependency", "binary", "package"),
    [
        ("stress-ng", "stress-ng", "stress-ng"),
        ("iproute2", "tc", "iproute2"),
        ("iptables", "iptables", "iptables"),
        ("procps", "pgrep", "procps"),
    ],
)
def test_auto_install_declared_system_commands(dependency, binary, package):
    target = MissingCommandTarget()

    Preflight(target).run("TestFault", [dependency], [], auto_install=True)

    assert f"which {binary} 2>/dev/null" in target.probes
    assert target.installs == [f"DEBIAN_FRONTEND=noninteractive apt-get install -y {package}"]


def test_auto_install_is_opt_in():
    target = MissingCommandTarget()

    with pytest.raises(PreflightError, match="auto_install=True"):
        Preflight(target).run("TestFault", ["iptables"], [], auto_install=False)

    assert target.installs == []


def test_systemd_is_not_auto_installed():
    target = MissingCommandTarget()

    with pytest.raises(PreflightError, match="cannot provision a service manager"):
        Preflight(target).run("ServiceFault", ["systemd"], [], auto_install=True)

    assert "which systemctl 2>/dev/null" in target.probes
    assert target.installs == []


def test_python_crontab_checks_its_import_name():
    class InstalledCrontabTarget(MissingCommandTarget):
        def run(self, cmd):
            self.probes.append(cmd)
            if cmd == "python3 -c 'import crontab' 2>/dev/null":
                return 0, "", ""
            return 1, "", ""

    target = InstalledCrontabTarget()

    Preflight(target).run("StorageCorrupt", [], ["python-crontab"], auto_install=False)

    assert target.probes == ["python3 -c 'import crontab' 2>/dev/null"]
