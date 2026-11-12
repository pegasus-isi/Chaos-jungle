"""Tests for the distributed reference multi-agent architecture."""

from __future__ import annotations

import json
from pathlib import Path

from chaos_jungle.faults.base import VerificationResult
from evaluation.distributed_faults import DistributedFaultPlan, apply_communication_fault
from evaluation.distributed_campaign import (
    CONTROLLED_INFRASTRUCTURE_FAULTS,
    _distributed_fault_config,
    _required_capabilities,
    _validate_distributed_args,
    build_parser,
    distributed_coordination_rows,
    distributed_infrastructure_rows,
    summarize_distributed_coordination,
    write_distributed_infrastructure_outputs,
)
from evaluation.distributed_entrypoint import run_role_request, run_score_request
from evaluation.distributed_runner import (
    DistributedNativeRunner,
    DistributedReferenceRunner,
    _RoleProcessKillHandle,
)
from evaluation.docker_runner import DockerExecution, DockerExecutionConfig, PreparedContainer, RunContext


def _simple_task() -> dict:
    return {
        "task_id": "toy/add",
        "benchmark": "toy",
        "prompt": "Write add(a, b).",
        "entry_point": "add",
        "test_code": "def check(candidate):\n    assert candidate(2, 3) == 5\ncheck(add)\n",
        "canonical_solution": "def add(a, b):\n    return a + b\n",
        "metadata": {"source": "bundled"},
    }


def _coordination_triplet() -> list[dict]:
    base = {
        "study_id": "study",
        "campaign_id": "campaign",
        "pair_id": "pair",
        "framework": "autogen-real",
        "topology": "linear",
        "benchmark": "humanevalplus",
        "task_id": "HumanEval/0",
        "seed": 17,
        "fault_type": "group_cpu_pressure",
    }
    direct = {**base, "run_id": "direct", "condition": "direct_baseline"}
    control = {**base, "run_id": "control", "condition": "cj_control"}
    fault = {
        **base,
        "run_id": "fault",
        "condition": "cj_fault",
        "validity": "valid",
        "group_valid": True,
        "partial_activation": False,
        "rollback_success": True,
        "verified_recovery": True,
        "lifecycle": {"verdict": "valid"},
        "distributed_group_evidence": {
            "verdict": "valid",
            "synchronization_valid": True,
            "activation_skew_ms": 4.0,
            "maximum_allowed_skew_ms": 100.0,
            "members": [
                {
                    "target_role": role,
                    "target_id": f"{role}-container",
                    "active_at": "2026-01-01T00:00:00Z",
                    "manifested": True,
                    "reverted_at": "2026-01-01T00:00:01Z",
                    "error": None,
                }
                for role in ("planner", "coder", "reviewer")
            ],
        },
    }
    return [direct, control, fault]


def test_coordination_summary_uses_group_members_not_partial_trace():
    records = _coordination_triplet()
    records[-1]["distributed_trace"] = [
        {"event_type": "agent_activation", "agent_role": "planner", "container_id": "planner-container"}
    ]

    rows = distributed_coordination_rows(records)
    summary = summarize_distributed_coordination(records)

    assert rows[0]["member_count"] == 3
    assert rows[0]["target_role_count"] == 3
    assert rows[0]["summary_consistent"] is True
    assert summary["complete_triplets"] == 1
    assert summary["group_valid_count"] == 1
    assert summary["summary_inconsistency_count"] == 0
    assert summary["verified_recovery_rate"] == 1.0


def test_distributed_role_entrypoint_emits_role_identity_in_dry_run(monkeypatch):
    monkeypatch.setenv("CJ_EVAL_BASE_URL", "http://example.test/v1")
    req = {
        "mode": "role",
        "role": "planner",
        "step_index": 0,
        "trace_id": "trace",
        "messages": [{"role": "user", "content": "Solve it."}],
        "seed": 1,
        "model_config": {"name": "fake", "base_url": "http://example.test/v1"},
        "execution_config": {"dry_run": True},
        "run_context": {"study_id": "study", "pair_id": "pair", "run_id": "run"},
    }

    result = run_role_request(req)

    assert result["executor_status"] == "ok"
    assert result["role"] == "planner"
    assert result["llm_calls"] == 1
    assert result["execution_trace"][0]["study_id"] == "study"
    assert result["execution_trace"][0]["agent_role"] == "planner"
    assert result["execution_trace"][0]["step_index"] == 0


def test_distributed_native_role_entrypoint_uses_native_labels_in_dry_run(monkeypatch):
    monkeypatch.setenv("CJ_EVAL_BASE_URL", "http://example.test/v1")
    req = {
        "mode": "role",
        "agent_system": "autogen-real",
        "role": "coder",
        "step_index": 1,
        "trace_id": "trace",
        "messages": [{"role": "user", "content": "Write add(a, b)."}],
        "seed": 1,
        "model_config": {"name": "fake", "base_url": "http://example.test/v1"},
        "execution_config": {"dry_run": True, "distributed_agent_system": "autogen-real"},
        "run_context": {"study_id": "study", "pair_id": "pair", "run_id": "run"},
    }

    result = run_role_request(req)

    assert result["executor_status"] == "ok"
    assert result["framework"] == "autogen-real"
    assert result["framework_native"] is True
    assert result["deployment_mode"] == "distributed"
    assert result["multi_agent_impl"] == "distributed-native-autogen-real-multi-agent"
    assert result["role"] == "coder"
    assert result["generated_code"]


def test_distributed_score_entrypoint_scores_inside_container_path():
    result = run_score_request({
        "mode": "score",
        "task": _simple_task(),
        "generated_code": "```python\ndef add(a, b):\n    return a + b\n```",
        "execution_config": {"score_timeout_s": 5},
    })

    assert result["executor_status"] == "ok"
    assert result["scorer_status"] == "ok"
    assert result["success"] == 1.0
    assert result["tests_total"] > 0


class _FakeDockerRunner:
    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.prepared: list[PreparedContainer] = []
        self.cleaned: list[str] = []
        self.running: set[str] = set()
        self.requests: list[dict] = []

    def prepare(self, *, run_context: RunContext, **_kwargs):
        role = Path(run_context.output_root).name
        input_dir = Path(run_context.output_root) / "input"
        output_dir = Path(run_context.output_root) / "output"
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        container = PreparedContainer(
            container_id=f"{role}-container",
            image="cj:test",
            image_digest="sha256:test",
            input_dir=str(input_dir),
            output_dir=str(output_dir),
            request_path="/cj/input/request.json",
            resource_limits={"cpus": 1.0, "memory": "2g"},
            created_at=0.0,
            artifact_input_dir=str(input_dir),
            artifact_output_dir=str(output_dir),
        )
        self.prepared.append(container)
        return container

    def is_running(self, container):
        return container.container_id in self.running

    def start_container(self, container):
        self.running.add(container.container_id)
        return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def exec_agent(self, container, *, request_path=None, output_path="/cj/output/result.json"):
        request_file = Path(container.input_dir) / Path(request_path or "request.json").name
        output_file = Path(container.output_dir) / Path(output_path).name
        req = json.loads(request_file.read_text(encoding="utf-8"))
        self.requests.append(req)
        mode = req.get("mode")
        if mode == "score":
            payload = {
                "executor_status": "ok",
                "mode": "score",
                "scorer_status": "ok",
                "success": 1.0,
                "tests_passed": 1,
                "tests_total": 1,
                "python_version": "3.12",
                "framework_versions": {"python": "3.12"},
            }
        else:
            role = req["role"]
            if role == "planner":
                content = "Plan: implement add."
            elif role == "coder":
                content = "```python\ndef add(a, b):\n    return a + b\n```"
            else:
                content = "ACCEPT"
            payload = {
                "executor_status": "ok",
                "mode": "role",
                "role": role,
                "step_index": req["step_index"],
                "content": content,
                "generated_code": "def add(a, b):\n    return a + b\n" if role == "coder" else "",
                "llm_calls": 1,
                "turns": 1,
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
                "python_version": "3.12",
                "framework_versions": {"python": "3.12"},
                "execution_trace": [{
                    "study_id": req["run_context"]["study_id"],
                    "run_id": req["run_context"]["run_id"],
                    "pair_id": req["run_context"]["pair_id"],
                    "agent_role": role,
                    "step_index": req["step_index"],
                    "timestamp": 1.0,
                    "trace_id": req["trace_id"],
                    "span_id": f"{role}-span",
                    "event_type": "sent_message",
                    "payload": {},
                }],
            }
        output_file.write_text(json.dumps(payload), encoding="utf-8")
        return DockerExecution(
            container=container,
            exec_command=["docker", "exec"],
            exit_code=0,
            stdout="",
            stderr="",
            timed_out=False,
            duration_s=0.1,
        )

    def _sync_staged_io(self, _container):
        return None

    def cleanup(self, container):
        self.cleaned.append(container.container_id)


def test_distributed_runner_uses_one_container_per_role(tmp_path):
    runner = DistributedReferenceRunner(DockerExecutionConfig(image="cj:test"))
    fake = _FakeDockerRunner(tmp_path)
    runner.docker_runner = fake
    context = RunContext(
        study_id="study",
        campaign_id="campaign",
        pair_id="pair",
        run_id="run",
        condition="direct_baseline",
        output_root=str(tmp_path / "distributed"),
    )

    result = runner.run(
        topology="linear",
        task=_simple_task(),
        seed=1,
        model_config={"name": "fake", "base_url": "http://example.test/v1"},
        execution_config={"dry_run": True, "score_timeout_s": 5},
        run_context=context,
    )

    assert result["deployment_mode"] == "distributed"
    assert result["agent_system"] == "distributed-reference-multi-agent"
    assert result["framework_native"] is False
    assert result["role_container_count"] == 3
    assert set(result["role_container_ids"]) == {"planner", "coder", "reviewer"}
    assert {event["container_id"] for event in result["distributed_trace"] if event.get("container_id")} >= {
        "planner-container",
        "coder-container",
        "reviewer-container",
    }
    assert result["scorer_status"] == "ok"
    assert result["success"] == 1.0
    assert sorted(fake.cleaned) == ["coder-container", "planner-container", "reviewer-container"]


def test_distributed_native_runner_uses_one_container_per_native_role(tmp_path):
    runner = DistributedNativeRunner(DockerExecutionConfig(image="cj:test"), native_system="autogen-real")
    fake = _FakeDockerRunner(tmp_path)
    runner.docker_runner = fake
    context = RunContext(
        study_id="study",
        campaign_id="campaign",
        pair_id="pair",
        run_id="run",
        condition="direct_baseline",
        output_root=str(tmp_path / "distributed-native"),
    )

    result = runner.run(
        topology="linear",
        task=_simple_task(),
        seed=1,
        model_config={"name": "fake", "base_url": "http://example.test/v1"},
        execution_config={"dry_run": True, "score_timeout_s": 5},
        run_context=context,
    )

    assert result["deployment_mode"] == "distributed"
    assert result["agent_system"] == "distributed-native-autogen-real-multi-agent"
    assert result["framework"] == "autogen-real"
    assert result["requested_framework"] == "autogen-real"
    assert result["framework_native"] is True
    assert result["multi_agent_impl"] == "distributed-native-autogen-real-multi-agent"
    assert result["role_container_count"] == 3
    assert set(result["role_container_ids"]) == {"planner", "coder", "reviewer"}
    role_requests = [req for req in fake.requests if req.get("mode") == "role"]
    assert role_requests
    assert {req["agent_system"] for req in role_requests} == {"autogen-real"}
    assert {req["execution_config"]["distributed_agent_system"] for req in role_requests} == {"autogen-real"}


def test_communication_fault_drop_records_missing_handoff():
    plan = DistributedFaultPlan.from_config({
        "fault_type": "message_drop",
        "from_role": "planner",
        "to_role": "coder",
        "severity": "medium",
    })

    deliveries, events = apply_communication_fault(
        plan=plan,
        content="plan",
        from_role="planner",
        to_role="coder",
    )

    assert deliveries == []
    assert plan.applied is True
    assert events[0]["event_type"] == "message_loss"
    assert events[0]["payload"]["from_role"] == "planner"


def test_communication_fault_corruption_mutates_only_matching_handoff():
    plan = DistributedFaultPlan.from_config({
        "fault_type": "message_corruption",
        "from_role": "coder",
        "to_role": "reviewer",
        "replacement": "bad code",
    })

    first, first_events = apply_communication_fault(
        plan=plan,
        content="plan",
        from_role="planner",
        to_role="coder",
    )
    second, second_events = apply_communication_fault(
        plan=plan,
        content="good code",
        from_role="coder",
        to_role="reviewer",
    )

    assert first == [("coder", "plan")]
    assert first_events == []
    assert second == [("reviewer", "bad code")]
    assert second_events[0]["event_type"] == "message_corruption"
    assert second_events[0]["payload"]["original_length"] == len("good code")


def test_coordinated_group_fault_has_distinct_distributed_layer():
    plan = DistributedFaultPlan.from_config({
        "fault_type": "group_cpu_pressure",
        "severity": "low",
        "duration_s": 5,
        "maximum_skew_ms": 75,
    })

    assert plan.is_active() is True
    assert plan.is_coordinated_group() is True
    assert plan.is_role_local() is False
    assert plan.is_communication() is False
    assert plan.layer() == "distributed_coordination"
    assert plan.as_record_parameters()["maximum_skew_ms"] == 75


def test_controlled_infrastructure_suite_has_exact_bounded_faults():
    assert CONTROLLED_INFRASTRUCTURE_FAULTS == {
        "role_network_delay",
        "role_network_loss",
        "role_memory_pressure",
        "role_process_kill",
    }
    for fault_name in CONTROLLED_INFRASTRUCTURE_FAULTS:
        plan = DistributedFaultPlan.from_config({
            "fault_type": fault_name,
            "target_role": "coder",
        })
        assert plan.is_role_local() is True
        assert plan.layer() == "distributed_role_infrastructure"


def test_controlled_infrastructure_parameters_are_explicit():
    args = build_parser().parse_args([
        "--docker-image", "cj:test",
        "--results-dir", "/tmp/results",
        "--fault-suite", "infrastructure_controlled",
        "--network-delay-ms", "175",
        "--network-loss-pct", "12",
        "--memory-pressure-mb", "320",
        "--process-trigger-timeout-s", "33",
    ])
    delay = _distributed_fault_config(args, "role_network_delay")
    loss = _distributed_fault_config(args, "role_network_loss")
    memory = _distributed_fault_config(args, "role_memory_pressure")
    process = _distributed_fault_config(args, "role_process_kill")

    assert delay["parameters"]["delay_ms"] == 175
    assert loss["parameters"]["loss_pct"] == 12
    assert memory["parameters"]["memory_bytes"] == "320M"
    assert process["parameters"]["trigger_timeout_s"] == 33
    assert process["parameters"]["process_pattern"] == "evaluation.distributed_entrypoint"
    assert _required_capabilities(["role_network_delay"]) == ["NET_ADMIN"]
    assert _required_capabilities(["role_memory_pressure", "role_process_kill"]) == []
    _validate_distributed_args(args)


def test_controlled_infrastructure_parameters_reject_unsafe_memory_pressure():
    args = build_parser().parse_args([
        "--docker-image", "cj:test",
        "--results-dir", "/tmp/results",
        "--fault-suite", "infrastructure_controlled",
        "--memory", "512m",
        "--memory-pressure-mb", "400",
    ])

    try:
        _validate_distributed_args(args)
    except ValueError as exc:
        assert "75%" in str(exc)
    else:
        raise AssertionError("unsafe memory pressure was accepted")


def test_role_process_kill_handle_preserves_trigger_and_recovery_evidence():
    class FakeTarget:
        def __init__(self):
            self.responses = [
                (0, "killed:123\n", ""),
                (0, "", ""),
            ]

        def run(self, _command):
            return self.responses.pop(0)

    target = FakeTarget()
    handle = _RoleProcessKillHandle(target, role="coder", timeout_s=1)
    handle._watch_and_kill()

    active = handle.verify_active(target)
    recovered = handle.verify_recovered(target)

    assert active.verified is True
    assert active.observed["killed_pid"] == "123"
    assert recovered.verified is True


def test_distributed_infrastructure_rows_require_exact_triplet_and_keep_measurements(tmp_path):
    base = {
        "study_id": "study",
        "pair_id": "pair",
        "framework": "autogen-real",
        "topology": "linear",
        "benchmark": "humanevalplus",
        "task_id": "HumanEval/0",
        "seed": 17,
        "fault_type": "role_network_delay",
        "fault_target_role": "coder",
    }
    records = [
        {
            **base, "run_id": "direct", "condition": "direct_baseline",
            "success": 1.0, "duration_s": 1.0, "scorer_status": "ok",
        },
        {
            **base, "run_id": "control", "condition": "cj_control",
            "success": 1.0, "duration_s": 1.1, "scorer_status": "ok",
        },
        {
            **base,
            "run_id": "fault",
            "condition": "cj_fault",
            "success": 1.0,
            "duration_s": 1.5,
            "scorer_status": "ok",
            "operational_success": True,
            "silent_failure": False,
            "validity": "valid",
            "lifecycle": {
                "configured": True, "activated": True, "triggered": True,
                "manifested": True, "reverted": True, "recovered": True,
            },
            "distributed_fault_evidence": [{
                "event_type": "fault_observed",
                "payload": {
                    "verified": True,
                    "reason": "delay observed",
                    "observed": {
                        "configured_delay_ms": 150.0,
                        "baseline_latency_ms": 0.5,
                        "observed_latency_ms": 151.0,
                    },
                },
            }],
        },
    ]

    rows = distributed_infrastructure_rows(records)

    assert len(rows) == 1
    assert rows[0]["configured_delay_ms"] == 150.0
    assert rows[0]["observed_latency_ms"] == 151.0
    assert rows[0]["recovered"] is True

    write_distributed_infrastructure_outputs(records, tmp_path)
    summary = json.loads(
        (tmp_path / "distributed_infrastructure_summary.json").read_text(encoding="utf-8")
    )
    fault_summary = summary["faults"]["role_network_delay"]
    assert fault_summary["scored_valid_pairs"] == 1
    assert fault_summary["baseline_eligible_pairs"] == 1
    assert fault_summary["conditional_robustness"] == 1.0


def test_distributed_runner_builds_one_coordinated_injection_per_role(monkeypatch, tmp_path):
    created_groups = []

    class FakeDockerTarget:
        def __init__(self, container_id, **_kwargs):
            self.container_id = container_id

    class FakeCPUStress:
        def __init__(self, cores, duration_s):
            self.cores = cores
            self.duration_s = duration_s

    class FakeMember:
        active_at = 1.0
        manifested = True

    class FakeEvidence:
        members = [FakeMember(), FakeMember(), FakeMember()]

        def to_dict(self):
            return {
                "verdict": "valid",
                "activation_skew_ms": 4.0,
                "members": [{"manifested": True} for _ in self.members],
            }

    class FakeGroupRunner:
        def __init__(self, group):
            self.group = group
            self.evidence = FakeEvidence()
            created_groups.append(group)

        def start(self):
            return None

        def stop(self):
            return self.evidence

    monkeypatch.setattr("evaluation.distributed_runner.DockerTarget", FakeDockerTarget)
    monkeypatch.setattr("evaluation.distributed_runner.CPUStress", FakeCPUStress)
    monkeypatch.setattr("evaluation.distributed_runner.InjectionGroupRunner", FakeGroupRunner)

    runner = DistributedReferenceRunner(DockerExecutionConfig(image="cj:test"))
    fake_docker = _FakeDockerRunner(tmp_path)
    runner.docker_runner = fake_docker
    context = RunContext(
        study_id="study",
        campaign_id="campaign",
        pair_id="pair",
        run_id="run",
        condition="cj_fault",
        output_root=str(tmp_path / "distributed"),
    )
    containers = runner._prepare_role_containers(
        task=_simple_task(),
        seed=1,
        model_config={"name": "fake", "base_url": "http://example.test/v1"},
        execution_config={"dry_run": True},
        run_context=context,
    )
    lifecycle = {
        "configured": True,
        "activated": False,
        "triggered": False,
        "manifested": False,
        "reverted": False,
        "recovered": False,
        "verdict": "pending",
        "details": "",
    }
    trace = []
    plan = DistributedFaultPlan.from_config({
        "fault_type": "group_cpu_pressure",
        "duration_s": 7,
        "maximum_skew_ms": 50,
    })

    group_runner = runner._maybe_apply_group_fault(
        containers,
        fault_plan=plan,
        lifecycle=lifecycle,
        trace=trace,
        run_context=context,
        trace_id="trace",
    )

    assert group_runner is not None
    assert len(created_groups) == 1
    group = created_groups[0]
    assert len(group.injections) == 3
    assert {injection.id for injection in group.injections} == {
        "planner-cpu-pressure",
        "coder-cpu-pressure",
        "reviewer-cpu-pressure",
    }
    assert all(injection.fault.duration_s == 7 for injection in group.injections)
    assert group.maximum_skew_ms == 50
    assert lifecycle["activated"] is True
    assert lifecycle["triggered"] is True
    assert lifecycle["manifested"] is True
    assert trace[-1]["event_type"] == "coordinated_fault_activated"
    assert set(trace[-1]["payload"]["target_roles"]) == set(containers)


def test_distributed_runner_records_communication_fault_lifecycle(tmp_path):
    runner = DistributedReferenceRunner(DockerExecutionConfig(image="cj:test"))
    runner.docker_runner = _FakeDockerRunner(tmp_path)
    context = RunContext(
        study_id="study",
        campaign_id="campaign",
        pair_id="pair",
        run_id="run",
        condition="cj_fault",
        output_root=str(tmp_path / "distributed"),
    )

    result = runner.run(
        topology="linear",
        task=_simple_task(),
        seed=1,
        model_config={"name": "fake", "base_url": "http://example.test/v1"},
        execution_config={
            "dry_run": True,
            "score_timeout_s": 5,
            "distributed_fault": {
                "fault_type": "message_drop",
                "from_role": "planner",
                "to_role": "coder",
            },
        },
        run_context=context,
    )

    lifecycle = result["distributed_fault_lifecycle"]
    assert result["executor_status"] == "error"
    assert lifecycle["activated"] is True
    assert lifecycle["triggered"] is True
    assert lifecycle["manifested"] is True
    assert lifecycle["verdict"] == "valid"
    assert any(e["event_type"] == "message_loss" for e in result["distributed_trace"])
    assert any(e["event_type"] == "handoff_failed" for e in result["distributed_trace"])


def test_distributed_runner_records_role_container_fault(monkeypatch, tmp_path):
    class FakeControllerTarget:
        def __init__(self, container_id, **_kwargs):
            self.container_id = container_id
            self.connected = False

        def connect(self):
            self.connected = True

        def disconnect(self):
            self.connected = False

        def run(self, _cmd):
            return 0, "ok", ""

    class FakeContainerKill:
        def __init__(self, container_id, action="kill"):
            self.container_id = container_id
            self.action = action

        def start(self, _target):
            return None

        def stop(self, _target):
            return None

        def verify_active(self, _target):
            return VerificationResult(verified=True, reason="container exited", observed={"status": "exited"})

        def verify_recovered(self, _target):
            return VerificationResult(verified=True, reason="container running", observed={"status": "running"})

    monkeypatch.setattr("evaluation.distributed_runner.DockerContainerControllerTarget", FakeControllerTarget)
    monkeypatch.setattr("evaluation.distributed_runner.ContainerKill", FakeContainerKill)

    runner = DistributedReferenceRunner(DockerExecutionConfig(image="cj:test"))
    runner.docker_runner = _FakeDockerRunner(tmp_path)
    context = RunContext(
        study_id="study",
        campaign_id="campaign",
        pair_id="pair",
        run_id="run",
        condition="cj_fault",
        output_root=str(tmp_path / "distributed"),
    )

    result = runner.run(
        topology="linear",
        task=_simple_task(),
        seed=1,
        model_config={"name": "fake", "base_url": "http://example.test/v1"},
        execution_config={
            "dry_run": True,
            "score_timeout_s": 5,
            "distributed_fault": {
                "fault_type": "role_container_kill",
                "target_role": "coder",
            },
        },
        run_context=context,
    )

    lifecycle = result["distributed_fault_lifecycle"]
    assert result["executor_status"] == "error"
    assert lifecycle["activated"] is True
    assert lifecycle["triggered"] is True
    assert lifecycle["manifested"] is True
    assert lifecycle["reverted"] is True
    assert lifecycle["recovered"] is True
    assert lifecycle["verdict"] == "valid"
    assert any(e["event_type"] == "fault_observed" and e["agent_role"] == "coder" for e in result["distributed_trace"])


def test_role_network_delay_requires_objective_latency_measurement(monkeypatch, tmp_path):
    class FakeTarget:
        def __init__(self, container_id, **_kwargs):
            self.container_id = container_id
            self.probes = 0

        def connect(self):
            return None

        def disconnect(self):
            return None

        def run(self, _command):
            self.probes += 1
            latency = 0.5 if self.probes == 1 else 151.0
            return 0, (
                "5 packets transmitted, 5 received, 0% packet loss\n"
                f"rtt min/avg/max/mdev = 0.1/{latency}/152.0/1.0 ms\n"
            ), ""

    class FakeNetworkDelay:
        def __init__(self, delay, iface):
            self.delay = delay
            self.iface = iface

        def start(self, _target):
            return None

        def stop(self, _target):
            return None

        def verify_active(self, _target):
            return VerificationResult(
                verified=True,
                reason="tc netem delay present",
                observed={"qdisc": "delay 150ms"},
            )

        def verify_recovered(self, _target):
            return VerificationResult(verified=True, reason="qdisc removed")

    monkeypatch.setattr("evaluation.distributed_runner.DockerTarget", FakeTarget)
    monkeypatch.setattr("evaluation.distributed_runner.NetworkDelay", FakeNetworkDelay)

    runner = DistributedReferenceRunner(DockerExecutionConfig(image="cj:test"))
    fake_docker = _FakeDockerRunner(tmp_path)
    runner.docker_runner = fake_docker
    context = RunContext(
        study_id="study", campaign_id="campaign", pair_id="pair", run_id="run",
        condition="cj_fault", output_root=str(tmp_path / "distributed"),
    )
    containers = runner._prepare_role_containers(
        task=_simple_task(), seed=1,
        model_config={"name": "fake", "base_url": "http://example.test/v1"},
        execution_config={"dry_run": True}, run_context=context,
    )
    fake_docker.running.add(containers["coder"].container_id)
    lifecycle = {
        "configured": True, "activated": False, "triggered": False,
        "manifested": False, "reverted": False, "recovered": False,
        "verdict": "pending", "details": "",
    }
    trace = []
    cleanups = []
    plan = DistributedFaultPlan.from_config({
        "fault_type": "role_network_delay",
        "target_role": "coder",
        "parameters": {"delay_ms": 150},
    })

    runner._maybe_apply_role_fault(
        containers, role="coder", fault_plan=plan, lifecycle=lifecycle,
        trace=trace, run_context=context, step=1, trace_id="trace",
        cleanups=cleanups,
    )

    assert lifecycle["manifested"] is True
    assert trace[-1]["payload"]["observed"]["configured_delay_ms"] == 150
    assert trace[-1]["payload"]["observed"]["observed_latency_ms"] == 151.0
    assert len(cleanups) == 1
