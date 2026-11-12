"""Local target privilege handling."""

import pytest

from chaos_jungle.targets import local


@pytest.mark.parametrize(
    ("euid", "expected"),
    [(0, "apt-get install -y stress-ng"), (1000, "sudo apt-get install -y stress-ng")],
)
def test_sudo_uses_existing_root_privileges(monkeypatch, euid, expected):
    calls = []

    def fake_run(_self, cmd):
        calls.append(cmd)
        return 0, "", ""

    monkeypatch.setattr(local.os, "geteuid", lambda: euid)
    monkeypatch.setattr(local.LocalTarget, "run", fake_run)

    assert local.LocalTarget().sudo("apt-get install -y stress-ng") == (0, "", "")
    assert calls == [expected]
