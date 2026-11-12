"""Dependency CLI planning and package-manager selection without host installs."""

from click.testing import CliRunner

from chaos_jungle.control.cli import main
from chaos_jungle.core.preflight import PKG_MAP, PKG_TO_BIN, Preflight


class PackageTarget:
    def __init__(self, distro="debian", managers=("apt-get",), commands=(), packages=()):
        self.distro = distro
        self.managers = set(managers)
        self.commands = set(commands)
        self.packages = set(packages)
        self.installs = []
        self.probes = []

    def run(self, cmd):
        self.probes.append(cmd)
        if cmd == "cat /etc/os-release 2>/dev/null":
            if self.distro == "rocky":
                return 0, 'ID=rocky\nID_LIKE="rhel centos fedora"\n', ""
            if self.distro == "centos":
                return 0, 'ID=centos\nID_LIKE="rhel fedora"\n', ""
            return 0, "ID=debian\nID_LIKE=debian\n", ""
        if cmd.startswith("which "):
            name = cmd.split()[1]
            found = name in self.managers or name in self.commands
            return (0, "/usr/bin/" + name, "") if found else (1, "", "")
        if cmd.startswith("(dpkg -s "):
            package = cmd.split("rpm -q ", 1)[1].split()[0]
            return (0, "installed", "") if package in self.packages else (1, "", "")
        if cmd == "python3 -c 'import crontab' 2>/dev/null":
            return (0, "", "") if "python-crontab" in self.packages else (1, "", "")
        return 1, "", ""

    def sudo(self, cmd):
        self.installs.append(cmd)
        package = cmd.split()[-1]
        self.packages.add(package)
        manager = "apt" if "apt-get" in cmd else "dnf" if "dnf" in cmd else "yum"
        for canonical, mapped in PKG_MAP.items():
            if mapped.get(manager) == package and PKG_TO_BIN.get(canonical):
                self.commands.add(PKG_TO_BIN[canonical])
        return 0, "", ""


def invoke(monkeypatch, target, *args, input=None):
    monkeypatch.setattr("chaos_jungle.control.cli._make_target", lambda _value: target)
    return CliRunner().invoke(main, ["deps", *args], input=input)


def test_debian_check_is_read_only(monkeypatch):
    target = PackageTarget()

    result = invoke(monkeypatch, target, "check", "--fault", "cpu")

    assert result.exit_code == 1, result.output
    assert "Package manager: apt" in result.output
    assert "stress-ng -> stress-ng" in result.output
    assert target.installs == []


def test_debian_install_requires_confirmation(monkeypatch):
    target = PackageTarget()

    result = invoke(monkeypatch, target, "install", "--fault", "cpu", input="n\n")

    assert result.exit_code != 0
    assert target.installs == []


def test_debian_install_selected_fault(monkeypatch):
    target = PackageTarget()

    result = invoke(monkeypatch, target, "install", "--fault", "cpu", "--yes")

    assert result.exit_code == 0, result.output
    assert target.installs == ["DEBIAN_FRONTEND=noninteractive apt-get install -y stress-ng"]
    assert "Installable system commands are ready." in result.output


def test_rpm_prefers_dnf_and_maps_package(monkeypatch):
    target = PackageTarget(distro="rocky", managers=("apt-get", "dnf"))

    result = invoke(monkeypatch, target, "install", "--fault", "NetworkDelay", "--yes")

    assert result.exit_code == 0, result.output
    assert "Package manager: dnf" in result.output
    assert target.installs == ["dnf install -y iproute"]


def test_rpm_yum_fallback(monkeypatch):
    target = PackageTarget(distro="centos", managers=("yum",))

    result = invoke(monkeypatch, target, "install", "--fault", "ProcessKill", "--yes")

    assert result.exit_code == 0, result.output
    assert target.installs == ["yum install -y procps-ng"]


def test_rpm_bpf_package_check_uses_mapped_name():
    target = PackageTarget(distro="rocky", managers=("dnf",), packages=("python3-bcc",))

    missing = Preflight(target).check_missing(["python3-bpfcc"])

    assert missing == []
    assert any("rpm -q python3-bcc" in probe for probe in target.probes)


def test_all_installs_declared_commands_but_not_external_services(monkeypatch):
    target = PackageTarget(distro="rocky", managers=("dnf",))

    result = invoke(monkeypatch, target, "install", "--all", "--yes")

    assert result.exit_code == 0, result.output
    assert "systemd: requires a running systemd" in result.output
    assert "docker-cli: Docker CLI and daemon access" in result.output
    assert "python-crontab" in result.output
    assert "dnf install -y iproute" in target.installs
    assert "dnf install -y python3-bcc" in target.installs
    assert not any("docker" in command or "systemd" in command for command in target.installs)


def test_unknown_fault_is_rejected_without_install(monkeypatch):
    target = PackageTarget()

    result = invoke(monkeypatch, target, "install", "--fault", "NotAFault", "--yes")

    assert result.exit_code != 0
    assert "Unknown fault" in result.output
    assert target.probes == []
    assert target.installs == []


def test_fault_and_all_are_mutually_exclusive(monkeypatch):
    target = PackageTarget()

    result = invoke(monkeypatch, target, "check", "--fault", "cpu", "--all")

    assert result.exit_code != 0
    assert "not both" in result.output
    assert target.probes == []
