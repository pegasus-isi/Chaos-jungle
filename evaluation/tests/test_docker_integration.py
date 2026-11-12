from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import pytest

from chaos_jungle.targets.docker import DockerTarget
from chaos_jungle.faults.network import NetworkDelay
from evaluation.distributed_runner import (
    _RoleProcessKillHandle,
    _container_memory_snapshot,
    _network_probe,
)
from evaluation.docker_runner import DockerAgentRunner, DockerExecutionConfig, RunContext


pytestmark = pytest.mark.docker


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(["docker", "version"], capture_output=True, text=True, timeout=10)
    return proc.returncode == 0


def _local_image() -> str | None:
    image = os.environ.get("CJ_EVAL_TEST_IMAGE", "python:3.12-slim")
    proc = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return image if proc.returncode == 0 else None


def _cj_eval_image() -> str | None:
    image = os.environ.get("CJ_EVAL_IMAGE")
    if not image:
        return None
    proc = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return image if proc.returncode == 0 else None


docker_available = pytest.mark.skipif(not _docker_available(), reason="Docker daemon is unavailable")


@docker_available
def test_docker_target_real_container_non_root_timeout_and_cleanup():
    image = _local_image()
    if not image:
        pytest.skip("CJ_EVAL_TEST_IMAGE or python:3.12-slim is not available locally")

    create = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--user",
            "1000:1000",
            "--cpus",
            "0.5",
            "--memory",
            "128m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            image,
            "sleep",
            "60",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert create.returncode == 0, create.stderr
    cid = create.stdout.strip()
    try:
        target = DockerTarget(cid, timeout_s=1)
        code, out, err = target.run("id -u")
        assert code == 0
        assert out.strip() == "1000"
        code, out, err = target.run("python -c 'import time; time.sleep(5)'")
        assert code == 124

        inspect = subprocess.run(
            ["docker", "inspect", cid, "--format", "{{json .HostConfig}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        host_config = json.loads(inspect.stdout)
        assert host_config["Memory"] > 0
        assert host_config["NanoCpus"] > 0
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, text=True, timeout=20)
    gone = subprocess.run(["docker", "inspect", cid], capture_output=True, text=True, timeout=10)
    assert gone.returncode != 0


@docker_available
def test_cj_evaluation_image_runs_structured_protocol(tmp_path):
    image = _cj_eval_image()
    if not image:
        pytest.skip("set CJ_EVAL_IMAGE to a built CJ evaluation image to run this test")

    runner = DockerAgentRunner(
        DockerExecutionConfig(
            image=image,
            timeout_s=60,
            preserve_io=True,
            cpus=0.5,
            memory="512m",
        )
    )
    result = runner.run(
        agent_system="autogen",
        topology="single",
        task={
            "task_id": "toy/0",
            "benchmark": "toy",
            "prompt": "Write a function named solution returning None.",
            "entry_point": "solution",
            "test_code": "assert solution() is None\n",
            "metadata": {"source": "bundled"},
        },
        seed=0,
        model_config={"name": "fake"},
        execution_config={"dry_run": True, "agent_level": "individual", "score_timeout_s": 2},
        run_context=RunContext(
            study_id="study",
            campaign_id="campaign",
            pair_id="pair",
            run_id="docker-image-protocol",
            condition="direct_baseline",
            output_root=str(tmp_path / "run"),
        ),
    )
    assert result.exit_code == 0, result.stderr
    assert result.result["executor_status"] == "ok"
    assert result.result["scorer_status"] == "ok"
    assert result.result["success"] == 1.0
    assert result.result["python_version"]
    assert result.image_digest


@docker_available
def test_controlled_role_infrastructure_faults_use_only_disposable_container():
    image = _cj_eval_image()
    if not image:
        pytest.skip("set CJ_EVAL_IMAGE to a built CJ evaluation image to run this test")

    create = subprocess.run(
        [
            "docker", "run", "-d", "--rm",
            "--user", "1000:1000",
            "--cpus", "0.5",
            "--memory", "512m",
            "--cap-drop", "ALL",
            "--cap-add", "NET_ADMIN",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=192m",
            image,
            "sleep", "120",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert create.returncode == 0, create.stderr
    cid = create.stdout.strip()
    target = DockerTarget(cid, timeout_s=20)
    delay = NetworkDelay("50ms", iface="eth0")
    try:
        target.connect()

        baseline = _network_probe(target, count=4)
        delay.start(target)
        assert delay.verify_active(target).verified is True
        delayed = _network_probe(target, count=4)
        assert isinstance(baseline["latency_ms"], float)
        assert isinstance(delayed["latency_ms"], float)
        assert delayed["latency_ms"] - baseline["latency_ms"] >= 25.0
        delay.stop(target)
        assert delay.verify_recovered(target).verified is True

        memory_before = _container_memory_snapshot(target)
        code, stdout, stderr = target.run(
            "cd /tmp && stress-ng --vm 1 --vm-bytes 64M --vm-keep --timeout 10s "
            ">/tmp/cj_memory_test.log 2>&1 & pid=$!; sleep 0.75; "
            "kill -0 $pid 2>/dev/null || { cat /tmp/cj_memory_test.log; exit 1; }; "
            "echo $pid"
        )
        assert code == 0, f"{stderr}\n{stdout}"
        stress_pid = stdout.strip().splitlines()[-1]
        time.sleep(1.0)
        memory_during = _container_memory_snapshot(target)
        assert memory_before["memory_current_bytes"] is not None
        assert memory_during["memory_current_bytes"] is not None
        assert (
            int(memory_during["memory_current_bytes"])
            - int(memory_before["memory_current_bytes"])
        ) >= 8 * 1024**2
        target.run(f"kill {stress_pid} 2>/dev/null || true")

        code, _, stderr = target.run(
            "python -c 'import time; time.sleep(20)' "
            "evaluation.distributed_entrypoint >/tmp/cj_process_test.log 2>&1 &"
        )
        assert code == 0, stderr
        process_fault = _RoleProcessKillHandle(target, role="coder", timeout_s=5)
        process_fault._watch_and_kill()
        active = process_fault.verify_active(target)
        assert active.verified is True
        assert active.observed["killed_pid"]
        assert process_fault.verify_recovered(target).verified is True
    finally:
        try:
            delay.stop(target)
        except Exception:
            pass
        target.disconnect()
        subprocess.run(
            ["docker", "rm", "-f", cid],
            capture_output=True,
            text=True,
            timeout=20,
        )
    assert subprocess.run(
        ["docker", "inspect", cid], capture_output=True, text=True, timeout=10
    ).returncode != 0
