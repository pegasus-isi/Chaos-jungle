"""Bounded CPU-stress smoke test inside the current CI job container."""

from __future__ import annotations

import argparse
import json
import shlex
from dataclasses import asdict
from pathlib import Path

from chaos_jungle import CPUStress, ChaosRunner, LocalTarget, SafetyPolicy, Scenario
from chaos_jungle.db.session_db import SessionDB
from chaos_jungle.metrics import CollectStrategy, MetricSet


def container_cpu_quota(
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    container_marker: Path = Path("/.dockerenv"),
) -> float:
    if not container_marker.exists():
        raise RuntimeError("CPU stress is restricted to a Docker job container")
    v2 = cgroup_root / "cpu.max"
    if v2.exists():
        quota, period = v2.read_text().split()[:2]
        if quota == "max":
            raise RuntimeError("CI job container has no CPU quota")
        cpus = int(quota) / int(period)
    else:
        root = cgroup_root / "cpu"
        quota = int((root / "cpu.cfs_quota_us").read_text())
        period = int((root / "cpu.cfs_period_us").read_text())
        if quota < 0:
            raise RuntimeError("CI job container has no CPU quota")
        cpus = quota / period
    if not 0 < cpus <= 1:
        raise RuntimeError("CPU stress requires a container quota of at most one CPU")
    return cpus


def cgroup_throttled_usec(cgroup_root: Path = Path("/sys/fs/cgroup")) -> int:
    path = cgroup_root / "cpu.stat"
    if not path.exists():
        path = cgroup_root / "cpu" / "cpu.stat"
    values = dict(line.split()[:2] for line in path.read_text().splitlines())
    if "throttled_usec" in values:
        return int(values["throttled_usec"])
    if "throttled_time" in values:
        return int(values["throttled_time"]) // 1000
    raise RuntimeError("Container CPU throttling counter is unavailable")


def run_experiment(output_dir: Path) -> None:
    quota_cpus = container_cpu_quota()
    output_dir.mkdir(parents=True, exist_ok=True)
    db = SessionDB(path=str(output_dir / "session.db"))
    target = LocalTarget()
    fault = CPUStress(cores=1, duration_s=8)
    runner = ChaosRunner(
        Scenario("ci-container-cpu-stress", [fault]),
        target,
        db=db,
        policy=SafetyPolicy(max_danger=1, max_duration_s=12, monitor_interval_s=0.5),
    )
    phase = {"fault": False, "active_checks": 0, "baseline_throttled": []}
    workload_path = shlex.quote(str(Path(__file__).with_name("resource_workload.py")))

    def workload() -> dict[str, float]:
        if phase["fault"]:
            active = fault.verify_active(target)
            assert active.verified, active.reason
            phase["active_checks"] += 1
        before = cgroup_throttled_usec()
        code, stdout, stderr = target.run("python {}".format(workload_path))
        throttled = max(0, cgroup_throttled_usec() - before)
        assert code == 0, stderr
        measurement = json.loads(stdout)
        if phase["fault"]:
            threshold = max(phase["baseline_throttled"], default=0) + 10_000
            if throttled > threshold:
                runner.record_exposure(
                    fault,
                    triggered=True,
                    manifested=True,
                    event_reached=True,
                    evidence={"cgroup_throttled_usec": throttled, "stress_worker_alive": True},
                )
        else:
            phase["baseline_throttled"].append(throttled)
        return {
            "duration_s": float(measurement["duration_s"]),
            "throughput_ops_per_s": float(measurement["throughput_ops_per_s"]),
            "cgroup_throttled_usec": float(throttled),
            "success": 1.0,
        }

    try:
        result = runner.measure(
            workload,
            n_baseline=2,
            n_fault=2,
            strategy=CollectStrategy.SNAPSHOT,
            metric_set=MetricSet.only(
                "duration_s", "throughput_ops_per_s", "cgroup_throttled_usec"
            ),
            on_fault_start=lambda _session_id: phase.update(fault=True),
        )
        exposure = runner.exposure_report()
        session = db.get_session(result.session_id)
        report = {
            "interpretation": "Bounded resource integration smoke test; not a performance estimate",
            "target": "current CI job container",
            "container_cpu_quota": quota_cpus,
            "fault_default_metrics": fault.default_metrics,
            "scenario": result.scenario,
            "session_id": result.session_id,
            "baseline": result.baseline,
            "fault": result.fault,
            "delta": result.delta,
            "raw_baseline": result.raw_baseline,
            "raw_fault": result.raw_fault,
            "collected_metrics": (
                asdict(result.collected_metrics) if result.collected_metrics else None
            ),
            "exposure_report": exposure,
            "fault_active_checks": phase["active_checks"],
            "session_status": session["status"] if session else None,
        }
        (output_dir / "metrics.json").write_text(json.dumps(report, indent=2) + "\n")
        (output_dir / "summary.txt").write_text(
            "Bounded CPU-stress smoke test; not a performance estimate.\n\n"
            + result.summary() + "\n"
        )
        print(result.summary())
        print("Container CPU quota:", quota_cpus)
        print("CPU-stress active checks:", phase["active_checks"])
        print("Exposure verdict:", exposure["verdict"] if exposure else "missing")
        print("Artifacts:", output_dir.resolve())

        assert phase["active_checks"] == 2
        assert all(row["success"] == 1 for row in result.raw_baseline + result.raw_fault)
        assert result.collected_metrics is not None
        assert "duration_s" in result.collected_metrics.delta
        assert "cgroup_throttled_usec" in result.collected_metrics.delta
        assert session is not None and session["status"] == "reverted", session
        assert exposure is not None and exposure["verdict"] == "valid", exposure
        assert all(item["recovered"] for item in exposure["faults"])
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    run_experiment(args.output_dir)
