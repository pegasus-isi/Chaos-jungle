"""CI experiment behavior and containment checks."""

import json

import pytest

from ci.run_real_experiment import run_experiment as run_model_experiment
from ci.run_resource_experiment import cgroup_throttled_usec, container_cpu_quota


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


def test_cpu_quota_requires_container_marker(tmp_path):
    (tmp_path / "cpu.max").write_text("100000 100000\n")
    with pytest.raises(RuntimeError, match="Docker job container"):
        container_cpu_quota(tmp_path, tmp_path / "missing-marker")


@pytest.mark.parametrize(
    ("quota", "accepted"),
    [("100000 100000", True), ("50000 100000", True),
     ("200000 100000", False), ("max 100000", False)],
)
def test_cpu_quota_requires_at_most_one_core(tmp_path, quota, accepted):
    marker = tmp_path / "dockerenv"
    marker.touch()
    (tmp_path / "cpu.max").write_text(quota + "\n")
    if accepted:
        assert 0 < container_cpu_quota(tmp_path, marker) <= 1
    else:
        with pytest.raises(RuntimeError, match="CPU quota|quota of at most"):
            container_cpu_quota(tmp_path, marker)


def test_cgroup_throttling_counter(tmp_path):
    (tmp_path / "cpu.stat").write_text("nr_throttled 4\nthrottled_usec 12345\n")
    assert cgroup_throttled_usec(tmp_path) == 12345
