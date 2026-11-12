"""CI experiment behavior and containment checks."""

import json
import os

import pytest

from ci.run_real_experiment import run_experiment as run_model_experiment
from ci.run_agent_use_case import run_case as run_agent_case
from ci.run_resource_experiment import (
    container_memory_mb,
    memory_headroom_mb,
    prepare_container,
    stress_process_stats,
)


@pytest.mark.parametrize(
    ("fault_name", "expected_status"),
    [("latency", 200), ("unavailable", 503), ("rate-limit", 429)],
)
def test_local_model_experiment(tmp_path, fault_name, expected_status):
    output_dir = tmp_path / fault_name
    run_model_experiment(output_dir, mode="local", fault_name=fault_name)

    report = json.loads((output_dir / "metrics.json").read_text())
    assert report["fault_name"] == fault_name
    assert report["exposure_report"]["verdict"] == "valid"
    assert report["session_status"] == "reverted"
    assert (output_dir / "session.db").is_file()
    assert (output_dir / "summary.txt").is_file()
    if expected_status == 200:
        assert report["fault"]["success"] == 1
    else:
        assert report["fault"]["http_{}_count".format(expected_status)] > 0


@pytest.mark.parametrize("case", ["tool-result", "tool-result-requests", "prompt-input"])
def test_agent_use_case(tmp_path, case):
    output_dir = tmp_path / case
    run_agent_case(output_dir, case)

    report = json.loads((output_dir / "metrics.json").read_text())
    assert report["observed_metrics"]["http_calls"] == 1
    assert report["exposure_report"]["verdict"] == "valid"
    assert report["evidence"] == {
        "request_modified": True,
        "restored": True,
        "fault_trace_count": 1,
        "trace_modified": True,
    }
    assert (output_dir / "session.db").is_file()
    assert (output_dir / "summary.txt").is_file()


def test_resource_stress_requires_container_marker(tmp_path, monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    with pytest.raises(RuntimeError, match="Docker or Kubernetes job container"):
        prepare_container(
            pin_cpu=True,
            container_marker=tmp_path / "missing-docker-marker",
            kubernetes_marker=tmp_path / "missing-kubernetes-marker",
        )


def test_resource_stress_accepts_kubernetes_pod(tmp_path, monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    monkeypatch.setattr(os, "nice", lambda _value: 10, raising=False)
    assert prepare_container(
        pin_cpu=False,
        container_marker=tmp_path / "missing-docker-marker",
        kubernetes_marker=tmp_path / "missing-kubernetes-marker",
    ) is None


def test_resource_stress_accepts_kubernetes_service_account_marker(tmp_path, monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(os, "nice", lambda _value: 10, raising=False)
    marker = tmp_path / "namespace"
    marker.touch()
    assert prepare_container(
        pin_cpu=False,
        container_marker=tmp_path / "missing-docker-marker",
        kubernetes_marker=marker,
    ) is None


def test_cpu_stress_pins_one_core_and_lowers_priority(tmp_path, monkeypatch):
    marker = tmp_path / "dockerenv"
    marker.touch()
    affinity = []
    priority = []
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {3, 7}, raising=False)
    monkeypatch.setattr(os, "sched_setaffinity", lambda _pid, cpus: affinity.append(cpus), raising=False)
    monkeypatch.setattr(os, "nice", lambda value: priority.append(value), raising=False)
    assert prepare_container(pin_cpu=True, container_marker=marker) == 3
    assert affinity == [{3}]
    assert priority == [10]


def test_memory_stress_keeps_available_cpus(tmp_path, monkeypatch):
    marker = tmp_path / "dockerenv"
    marker.touch()
    monkeypatch.setattr(os, "sched_setaffinity", lambda *_args: pytest.fail("CPU pinned"), raising=False)
    monkeypatch.setattr(os, "nice", lambda _value: 10, raising=False)
    assert prepare_container(pin_cpu=False, container_marker=marker) is None


def test_stress_process_stats(tmp_path):
    worker = tmp_path / "123"
    worker.mkdir()
    (worker / "comm").write_text("stress-ng-vm\n")
    fields = ["R"] + ["0"] * 10 + ["12", "5"]
    (worker / "stat").write_text("123 (stress-ng-vm) " + " ".join(fields) + "\n")
    (worker / "statm").write_text("1000 256 0 0 0 0 0\n")
    ticks, rss_mb = stress_process_stats(tmp_path)[123]
    assert ticks == 17
    assert rss_mb == 256 * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)


def test_memory_headroom(tmp_path):
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "memory.max").write_text(str(1024 * 1024 * 1024) + "\n")
    (cgroup / "memory.current").write_text(str(256 * 1024 * 1024) + "\n")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable: 1048576 kB\n")
    assert container_memory_mb(cgroup) == 256
    assert memory_headroom_mb(cgroup, meminfo) == 768
    (cgroup / "memory.current").write_text(str(900 * 1024 * 1024) + "\n")
    with pytest.raises(RuntimeError, match="256 MiB"):
        memory_headroom_mb(cgroup, meminfo)


def test_memory_headroom_cgroup_v1(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    (memory / "memory.limit_in_bytes").write_text(str(1024 * 1024 * 1024) + "\n")
    (memory / "memory.usage_in_bytes").write_text(str(512 * 1024 * 1024) + "\n")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemAvailable: 1048576 kB\n")
    assert memory_headroom_mb(tmp_path, meminfo) == 512
