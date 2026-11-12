"""Bounded CJ resource smoke tests inside the current CI job container."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import time
from dataclasses import asdict
from pathlib import Path

from chaos_jungle import CPUStress, ChaosRunner, LocalTarget, MemoryStress, SafetyPolicy, Scenario
from chaos_jungle.db.session_db import SessionDB
from chaos_jungle.metrics import CollectStrategy, MetricSet


def prepare_container(
    pin_cpu: bool,
    container_marker: Path = Path("/.dockerenv"),
    kubernetes_marker: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace"),
) -> int | None:
    is_kubernetes = bool(os.environ.get("KUBERNETES_SERVICE_HOST")) or kubernetes_marker.exists()
    if not container_marker.exists() and not is_kubernetes:
        raise RuntimeError("Resource stress is restricted to a Docker or Kubernetes job container")
    cpu = None
    if pin_cpu:
        allowed_cpus = os.sched_getaffinity(0)
        if not allowed_cpus:
            raise RuntimeError("CI job container has no usable CPU affinity")
        cpu = min(allowed_cpus)
        os.sched_setaffinity(0, {cpu})
    os.nice(10)
    return cpu


def stress_process_stats(proc_root: Path = Path("/proc")) -> dict[int, tuple[int, float]]:
    stats = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if not (entry / "comm").read_text().startswith("stress-ng"):
                continue
            _, _, stat_tail = (entry / "stat").read_text().rpartition(") ")
            fields = stat_tail.split()
            if fields[0] != "Z":
                cpu_ticks = int(fields[11]) + int(fields[12])
                resident_pages = int((entry / "statm").read_text().split()[1])
                rss_mb = resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
                stats[int(entry.name)] = (cpu_ticks, rss_mb)
        except (FileNotFoundError, PermissionError):
            continue
    return stats


def container_memory_mb(cgroup_root: Path = Path("/sys/fs/cgroup")) -> float:
    path = cgroup_root / "memory.current"
    if not path.exists():
        path = cgroup_root / "memory" / "memory.usage_in_bytes"
    return int(path.read_text()) / (1024 * 1024)


def memory_headroom_mb(
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    meminfo: Path = Path("/proc/meminfo"),
) -> float:
    available_kb = next(
        int(line.split()[1]) for line in meminfo.read_text().splitlines()
        if line.startswith("MemAvailable:")
    )
    headroom_mb = available_kb / 1024
    limit_file = cgroup_root / "memory.max"
    if not limit_file.exists():
        limit_file = cgroup_root / "memory" / "memory.limit_in_bytes"
    if limit_file.exists():
        limit = limit_file.read_text().strip()
        if limit != "max":
            headroom_mb = min(
                headroom_mb,
                int(limit) / (1024 * 1024) - container_memory_mb(cgroup_root),
            )
    if headroom_mb < 256:
        raise RuntimeError("Memory stress requires at least 256 MiB of available memory")
    return headroom_mb


def run_experiment(output_dir: Path, fault_name: str = "cpu") -> None:
    if fault_name not in {"cpu", "memory"}:
        raise ValueError("Unsupported resource fault: {}".format(fault_name))
    affinity_cpu = prepare_container(pin_cpu=fault_name == "cpu")
    headroom_mb = memory_headroom_mb() if fault_name == "memory" else None
    output_dir.mkdir(parents=True, exist_ok=True)
    db = SessionDB(path=str(output_dir / "session.db"))
    target = LocalTarget()
    fault = (
        CPUStress(cores=1, duration_s=5)
        if fault_name == "cpu" else MemoryStress(mb=64, duration_s=5)
    )
    fault.preflight(target, auto_install=True)
    runner = ChaosRunner(
        Scenario("ci-container-{}-stress".format(fault_name), [fault]),
        target,
        db=db,
        auto_install=True,
        policy=SafetyPolicy(max_danger=1, max_duration_s=8, monitor_interval_s=0.5),
    )
    phase = {"fault": False, "active_checks": 0}
    workload_path = shlex.quote(str(Path(__file__).with_name("resource_workload.py")))

    def workload() -> dict[str, float]:
        if phase["fault"]:
            active = fault.verify_active(target)
            assert active.verified, active.reason
            phase["active_checks"] += 1
        before = stress_process_stats()
        code, stdout, stderr = target.run("python {}".format(workload_path))
        after = stress_process_stats()
        stress_cpu_ms = sum(
            max(0, ticks - before.get(pid, (ticks, 0))[0])
            for pid, (ticks, _) in after.items()
        ) * 1000 / os.sysconf("SC_CLK_TCK")
        stress_rss_mb = max(
            sum(rss for _, rss in before.values()),
            sum(rss for _, rss in after.values()),
        )
        assert code == 0, stderr
        measurement = json.loads(stdout)
        memory_mb = container_memory_mb() if fault_name == "memory" else None
        manifested = stress_cpu_ms > 0 if fault_name == "cpu" else stress_rss_mb >= 16
        if phase["fault"] and manifested:
            evidence = (
                {"stress_cpu_ms": stress_cpu_ms, "affinity_cpu": affinity_cpu}
                if fault_name == "cpu"
                else {"stress_rss_mb": stress_rss_mb, "container_memory_mb": memory_mb}
            )
            runner.record_exposure(
                fault,
                triggered=True,
                manifested=True,
                event_reached=True,
                evidence=evidence,
            )
        metrics = {
            "duration_s": float(measurement["duration_s"]),
            "throughput_ops_per_s": float(measurement["throughput_ops_per_s"]),
            "success": 1.0,
        }
        if fault_name == "cpu":
            metrics["stress_cpu_ms"] = stress_cpu_ms
        else:
            metrics["stress_rss_mb"] = stress_rss_mb
            metrics["container_memory_mb"] = memory_mb
        return metrics

    try:
        selected_metrics = (
            ("duration_s", "throughput_ops_per_s", "stress_cpu_ms")
            if fault_name == "cpu"
            else ("duration_s", "throughput_ops_per_s", "stress_rss_mb", "container_memory_mb")
        )
        result = runner.measure(
            workload,
            n_baseline=2,
            n_fault=2,
            strategy=CollectStrategy.SNAPSHOT,
            metric_set=MetricSet.only(*selected_metrics),
            on_fault_start=lambda _session_id: phase.update(fault=True),
        )
        exposure = runner.exposure_report()
        session = db.get_session(result.session_id)
        report = {
            "interpretation": "Bounded resource integration smoke test; not a performance estimate",
            "target": "current CI job container",
            "fault_name": fault_name,
            "affinity_cpu": affinity_cpu,
            "process_nice": os.nice(0),
            "memory_headroom_mb": headroom_mb,
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
            "Bounded {}-stress smoke test; not a performance estimate.\n\n".format(fault_name)
            + result.summary() + "\n"
        )
        print(result.summary())
        print("Fault case:", fault_name)
        print("Pinned CPU:", affinity_cpu)
        print("Process nice:", os.nice(0))
        print("Fault active checks:", phase["active_checks"])
        print("Exposure verdict:", exposure["verdict"] if exposure else "missing")
        print("Artifacts:", output_dir.resolve())

        deadline = time.monotonic() + 5
        while stress_process_stats() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert phase["active_checks"] == 2
        assert all(row["success"] == 1 for row in result.raw_baseline + result.raw_fault)
        assert result.collected_metrics is not None
        assert "duration_s" in result.collected_metrics.delta
        exposure_metric = "stress_cpu_ms" if fault_name == "cpu" else "stress_rss_mb"
        assert exposure_metric in result.collected_metrics.delta
        assert all(row[exposure_metric] == 0 for row in result.raw_baseline)
        minimum_exposure = 0 if fault_name == "cpu" else 16
        assert all(row[exposure_metric] > minimum_exposure for row in result.raw_fault)
        assert not stress_process_stats(), "Resource stress worker remained active after cleanup"
        assert session is not None and session["status"] == "reverted", session
        assert exposure is not None and exposure["verdict"] == "valid", exposure
        assert all(item["recovered"] for item in exposure["faults"])
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fault", choices=("cpu", "memory"), default="cpu")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    run_experiment(args.output_dir, fault_name=args.fault)
