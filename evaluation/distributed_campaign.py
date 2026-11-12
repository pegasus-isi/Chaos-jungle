"""CLI for distributed reference and native multi-agent smoke experiments."""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import os
import re
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

from chaos_jungle.db import SessionDB
from evaluation.distributed_faults import (
    COMMUNICATION_FAULTS,
    COORDINATED_GROUP_FAULTS,
    ROLE_LOCAL_FAULTS,
    DistributedFaultPlan,
)
from evaluation.distributed_runner import (
    REFERENCE_DISTRIBUTED_SYSTEM,
    DistributedNativeRunner,
    DistributedReferenceRunner,
    benchmark_task_to_dict,
    distributed_agent_labels,
)
from evaluation.docker_runner import DockerExecutionConfig, RunContext, redact_secrets
from evaluation.experiments.protocol import _CJ_COMMIT, _CJ_SOURCE_PROVENANCE
from evaluation.extended_campaign import _proxy_evidence
from evaluation.output import generate_all_outputs
from evaluation.run import _load_task_subset, _publication_model_config
from evaluation.schema import OUTPUT_SCHEMA_VERSION
from evaluation.study_protocol import (
    PublicationStudyOrchestrator,
    default_experiment_definition,
    make_pair_id,
)
from evaluation.task_complexity import classify_task_complexity


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run a container-per-role distributed multi-agent smoke")
    p.add_argument("--docker-image", required=True)
    p.add_argument("--results-dir", required=True)
    p.add_argument(
        "--agent-system",
        choices=[REFERENCE_DISTRIBUTED_SYSTEM, "autogen-real", "langgraph-real", "crewai-real"],
        default=REFERENCE_DISTRIBUTED_SYSTEM,
        help="Reference workflow or native framework role implementation",
    )
    p.add_argument("--benchmark", choices=["humanevalplus", "mbppplus"], default="humanevalplus")
    p.add_argument("--benchmark-subset", choices=["auto", "smoke", "development", "full"], default="smoke")
    p.add_argument("--tasks", type=int, default=1)
    p.add_argument("--seed", type=int, default=301)
    p.add_argument(
        "--task-selection-seed",
        type=int,
        default=17,
        help="Fixed seed used only to select the task manifest across execution seeds",
    )
    p.add_argument("--topology", choices=["linear", "closed_loop"], default="linear")
    p.add_argument("--model", default=os.environ.get("CJ_EVAL_MODEL") or os.environ.get("LLM_MODEL") or "gpt-4o-mini")
    p.add_argument("--base-url", default=os.environ.get("CJ_EVAL_BASE_URL") or os.environ.get("LLM_BASE_URL") or "")
    p.add_argument("--container-base-url", default=os.environ.get("CJ_EVAL_CONTAINER_BASE_URL") or "")
    p.add_argument("--api-key", default=os.environ.get("CJ_EVAL_API_KEY") or os.environ.get("LLM_API_KEY") or "dummy")
    p.add_argument("--env-file")
    p.add_argument("--container-timeout", type=float, default=420.0)
    p.add_argument("--score-timeout", type=float, default=25.0)
    p.add_argument("--max-turns", type=int, default=8)
    p.add_argument("--cpus", type=float, default=1.0)
    p.add_argument("--memory", default="2g")
    p.add_argument("--proxy-port", type=int, default=20600)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--study-id")
    p.add_argument("--triplet", action="store_true", help="Run direct/control/fault triplets")
    p.add_argument(
        "--fault",
        default="none",
        help="Distributed fault name, comma-list, or suite name",
    )
    p.add_argument(
        "--fault-suite",
        choices=[
            "smoke", "communication", "role_local", "coordination",
            "infrastructure_controlled", "all",
        ],
        help="Named distributed fault suite",
    )
    p.add_argument("--fault-severity", default="medium", choices=["low", "medium", "high"])
    p.add_argument("--fault-duration-s", type=float, default=300.0)
    p.add_argument("--maximum-skew-ms", type=float, default=100.0)
    p.add_argument("--network-delay-ms", type=float, default=150.0)
    p.add_argument("--network-loss-pct", type=float, default=10.0)
    p.add_argument("--memory-pressure-mb", type=int, default=256)
    p.add_argument("--process-trigger-timeout-s", type=float, default=45.0)
    p.add_argument("--fault-target-role", default="coder")
    p.add_argument("--fault-from-role", default="planner")
    p.add_argument("--fault-to-role", default="coder")
    return p


DISTRIBUTED_FAULT_SUITES = {
    "smoke": ["message_delay"],
    "communication": sorted(COMMUNICATION_FAULTS),
    "role_local": sorted(ROLE_LOCAL_FAULTS),
    "coordination": sorted(COORDINATED_GROUP_FAULTS),
    "infrastructure_controlled": [
        "role_network_delay",
        "role_network_loss",
        "role_memory_pressure",
        "role_process_kill",
    ],
    "all": sorted(COMMUNICATION_FAULTS | ROLE_LOCAL_FAULTS | COORDINATED_GROUP_FAULTS),
}


def _distributed_fault_names(args: argparse.Namespace) -> list[str]:
    if args.fault_suite and args.fault not in ("", "none"):
        raise SystemExit("Use either --fault or --fault-suite, not both")
    if args.fault_suite:
        return list(DISTRIBUTED_FAULT_SUITES[args.fault_suite])
    raw = str(args.fault or "none").strip()
    if raw in ("", "none"):
        return ["none"]
    if raw in DISTRIBUTED_FAULT_SUITES:
        return list(DISTRIBUTED_FAULT_SUITES[raw])
    names = [part.strip() for part in raw.split(",") if part.strip()]
    supported = set().union(*DISTRIBUTED_FAULT_SUITES.values())
    unsupported = [name for name in names if name not in supported]
    if unsupported:
        raise SystemExit(f"unsupported distributed fault(s): {', '.join(unsupported)}")
    return names


def _distributed_fault_config(args: argparse.Namespace, fault_name: str) -> dict[str, Any]:
    if fault_name in ("", "none"):
        return {}
    config: dict[str, Any] = {
        "fault_type": fault_name,
        "severity": args.fault_severity,
    }
    if fault_name in COMMUNICATION_FAULTS:
        config.update({
            "from_role": args.fault_from_role,
            "to_role": args.fault_to_role,
        })
        if fault_name == "message_misrouting":
            config["target_role"] = args.fault_target_role
            config.setdefault("misroute_to", args.fault_target_role)
    if fault_name in ROLE_LOCAL_FAULTS:
        config["target_role"] = args.fault_target_role
        parameters: dict[str, Any] = {"duration_s": args.fault_duration_s}
        if fault_name == "role_network_delay":
            parameters["delay_ms"] = args.network_delay_ms
        elif fault_name == "role_network_loss":
            parameters["loss_pct"] = args.network_loss_pct
        elif fault_name == "role_memory_pressure":
            parameters["memory_bytes"] = f"{args.memory_pressure_mb}M"
            parameters["memory_mb"] = args.memory_pressure_mb
        elif fault_name == "role_process_kill":
            parameters["trigger_timeout_s"] = args.process_trigger_timeout_s
            parameters["process_pattern"] = "evaluation.distributed_entrypoint"
            parameters["signal"] = "KILL"
        config["parameters"] = parameters
    if fault_name in COORDINATED_GROUP_FAULTS:
        config["parameters"] = {
            "duration_s": args.fault_duration_s,
            "maximum_skew_ms": args.maximum_skew_ms,
        }
    return config


def _required_capabilities(fault_names: list[str]) -> list[str]:
    """Return the minimum container capabilities required by the selected faults."""
    if any(name in {"role_network_delay", "role_network_loss"} for name in fault_names):
        return ["NET_ADMIN"]
    return []


def _memory_limit_mib(value: str) -> float | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)b?\s*", value, re.IGNORECASE)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).lower()
    multiplier = {"": 1 / (1024**2), "k": 1 / 1024, "m": 1, "g": 1024, "t": 1024**2}
    return amount * multiplier[unit]


def _validate_distributed_args(args: argparse.Namespace) -> None:
    """Reject unsafe or scientifically ambiguous campaign parameters."""
    if args.tasks < 1:
        raise ValueError("--tasks must be at least 1")
    if args.cpus <= 0:
        raise ValueError("--cpus must be greater than 0")
    if args.fault_duration_s <= 0:
        raise ValueError("--fault-duration-s must be greater than 0")
    if args.network_delay_ms <= 0:
        raise ValueError("--network-delay-ms must be greater than 0")
    if not 0 < args.network_loss_pct <= 100:
        raise ValueError("--network-loss-pct must be in (0, 100]")
    if args.memory_pressure_mb < 8:
        raise ValueError("--memory-pressure-mb must be at least 8 MiB")
    if args.process_trigger_timeout_s <= 0:
        raise ValueError("--process-trigger-timeout-s must be greater than 0")
    for option, role in (
        ("--fault-target-role", args.fault_target_role),
        ("--fault-from-role", args.fault_from_role),
        ("--fault-to-role", args.fault_to_role),
    ):
        if role not in {"planner", "coder", "reviewer"}:
            raise ValueError(f"{option} must be planner, coder, or reviewer")
    limit_mib = _memory_limit_mib(args.memory)
    if limit_mib is None:
        raise ValueError("--memory must use a Docker size such as 512m or 2g")
    if args.memory_pressure_mb > limit_mib * 0.75:
        raise ValueError(
            "--memory-pressure-mb must not exceed 75% of the container memory limit"
        )


def _task_complexity_fields(task: Any) -> dict[str, Any]:
    c = classify_task_complexity(
        benchmark=task.benchmark,
        task_id=task.task_id,
        prompt=task.prompt,
        test_code=task.test_code,
        canonical_solution=task.canonical_solution,
    )
    return {
        "task_complexity": c.complexity_proxy,
        "complexity_proxy": c.complexity_proxy,
        "complexity_proxy_kind": c.complexity_proxy_kind,
        "complexity_score": c.complexity_score,
        "complexity_prompt_chars": c.prompt_chars,
        "complexity_prompt_words": c.prompt_words,
        "complexity_test_chars": c.test_chars,
        "complexity_test_assertions": c.test_assertions,
        "complexity_reference_solution_chars": c.reference_solution_chars,
        "complexity_reference_solution_lines": c.reference_solution_lines,
        "complexity_required_reasoning_steps": c.required_reasoning_steps,
        "complexity_algorithmic_category": c.algorithmic_category,
        "complexity_classification_rule": c.complexity_classification_rule,
    }


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(redact_secrets(record), sort_keys=True) + "\n")


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def distributed_coordination_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return E7 lifecycle rows derived only from authoritative group evidence."""
    rows: list[dict[str, Any]] = []
    for record in records:
        evidence = record.get("distributed_group_evidence")
        if record.get("condition") != "cj_fault" or not isinstance(evidence, dict):
            continue
        members = [member for member in evidence.get("members", []) if isinstance(member, dict)]
        roles = sorted({str(member.get("target_role")) for member in members if member.get("target_role")})
        target_ids = sorted({str(member.get("target_id")) for member in members if member.get("target_id")})
        activated = bool(members) and all(member.get("active_at") and not member.get("error") for member in members)
        manifested = bool(members) and all(member.get("manifested") is True for member in members)
        reverted = bool(members) and all(member.get("reverted_at") for member in members)
        synchronization_valid = evidence.get("synchronization_valid") is True
        evidence_valid = evidence.get("verdict") == "valid"
        derived_valid = activated and manifested and reverted and synchronization_valid and evidence_valid
        top_level_valid = record.get("group_valid") is True
        lifecycle = record.get("lifecycle") if isinstance(record.get("lifecycle"), dict) else {}
        rows.append({
            "study_id": record.get("study_id", ""),
            "campaign_id": record.get("campaign_id", ""),
            "pair_id": record.get("pair_id", ""),
            "run_id": record.get("run_id", ""),
            "framework": record.get("framework", ""),
            "topology": record.get("topology", ""),
            "benchmark": record.get("benchmark", ""),
            "task_id": record.get("task_id", ""),
            "seed": record.get("seed", ""),
            "fault_type": record.get("fault_type", ""),
            "member_count": len(members),
            "target_role_count": len(roles),
            "target_roles": json.dumps(roles),
            "target_ids": json.dumps(target_ids),
            "activated_all": activated,
            "manifested_all": manifested,
            "reverted_all": reverted,
            "verified_recovery": record.get("verified_recovery") is True,
            "partial_activation": record.get("partial_activation") is True,
            "rollback_success": record.get("rollback_success") is True,
            "synchronization_valid": synchronization_valid,
            "activation_skew_ms": evidence.get("activation_skew_ms"),
            "maximum_allowed_skew_ms": evidence.get("maximum_allowed_skew_ms"),
            "evidence_verdict": evidence.get("verdict", ""),
            "lifecycle_verdict": lifecycle.get("verdict", record.get("validity", "")),
            "top_level_group_valid": top_level_valid,
            "derived_group_valid": derived_valid,
            "summary_consistent": top_level_valid == derived_valid,
        })
    return rows


def summarize_distributed_coordination(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize E7 without inferring target membership from partial agent traces."""
    rows = distributed_coordination_rows(records)
    fault_records = [record for record in records if record.get("condition") == "cj_fault"]
    by_pair: dict[str, set[str]] = {}
    for record in records:
        pair_id = str(record.get("pair_id") or "")
        condition = str(record.get("condition") or "")
        if pair_id and condition:
            by_pair.setdefault(pair_id, set()).add(condition)
    complete_triplets = sum(
        {"direct_baseline", "cj_control", "cj_fault"} <= conditions
        for conditions in by_pair.values()
    )
    skews = [float(row["activation_skew_ms"]) for row in rows if isinstance(row.get("activation_skew_ms"), (int, float))]
    count = len(rows)

    def rate(field: str) -> float | None:
        return sum(row.get(field) is True for row in rows) / count if count else None

    return {
        "metric_scope": "coordinated multi-target lifecycle evidence",
        "evidence_source": "distributed_group_evidence",
        "raw_records": len(records),
        "fault_records": len(fault_records),
        "group_evidence_records": count,
        "complete_triplets": complete_triplets,
        "missing_group_evidence_records": len(fault_records) - count,
        "unique_tasks": len({row["task_id"] for row in rows}),
        "execution_seeds": sorted({row["seed"] for row in rows}),
        "group_valid_count": sum(row["derived_group_valid"] is True for row in rows),
        "partial_activation_count": sum(row["partial_activation"] is True for row in rows),
        "summary_inconsistency_count": sum(row["summary_consistent"] is not True for row in rows),
        "activation_success_rate": rate("activated_all"),
        "manifestation_rate": rate("manifested_all"),
        "synchronization_valid_rate": rate("synchronization_valid"),
        "rollback_success_rate": rate("rollback_success"),
        "verified_recovery_rate": rate("verified_recovery"),
        "activation_skew_p50_ms": statistics.median(skews) if skews else None,
        "activation_skew_p95_ms": _percentile(skews, 0.95),
        "activation_skew_max_ms": max(skews) if skews else None,
    }


def write_distributed_coordination_outputs(records: list[dict[str, Any]], results_dir: Path) -> None:
    """Write the dedicated E7 summary and auditable per-group evidence table."""
    rows = distributed_coordination_rows(records)
    summary_path = results_dir / "distributed_coordination_summary.json"
    summary_path.write_text(
        json.dumps(redact_secrets(summarize_distributed_coordination(records)), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    csv_path = results_dir / "distributed_coordination_runs.csv"
    fields = list(rows[0].keys()) if rows else [
        "study_id", "pair_id", "run_id", "framework", "topology", "benchmark",
        "task_id", "seed", "fault_type", "member_count", "target_role_count",
        "target_roles", "activated_all", "manifested_all", "reverted_all",
        "verified_recovery", "partial_activation", "rollback_success",
        "synchronization_valid", "activation_skew_ms", "maximum_allowed_skew_ms",
        "evidence_verdict", "lifecycle_verdict", "top_level_group_valid",
        "derived_group_valid", "summary_consistent",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


CONTROLLED_INFRASTRUCTURE_FAULTS = {
    "role_network_delay",
    "role_network_loss",
    "role_memory_pressure",
    "role_process_kill",
}


def distributed_infrastructure_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten paired task outcomes and objective role-infrastructure evidence."""
    pairs: dict[str, dict[str, dict[str, Any]]] = {}
    for record in records:
        pair_id = str(record.get("pair_id") or "")
        condition = str(record.get("condition") or "")
        if pair_id and condition:
            pairs.setdefault(pair_id, {})[condition] = record

    rows: list[dict[str, Any]] = []
    for pair_id, triplet in sorted(pairs.items()):
        fault = triplet.get("cj_fault")
        if not fault or fault.get("fault_type") not in CONTROLLED_INFRASTRUCTURE_FAULTS:
            continue
        lifecycle = fault.get("lifecycle") if isinstance(fault.get("lifecycle"), dict) else {}
        observed: dict[str, Any] = {}
        fault_event = ""
        for event in fault.get("distributed_fault_evidence") or []:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("event_type") or "")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event_type == "fault_observed":
                fault_event = event_type
                candidate = payload.get("observed")
                if isinstance(candidate, dict):
                    observed.update(candidate)
                observed.setdefault("verified", payload.get("verified"))
                observed.setdefault("reason", payload.get("reason"))
        direct = triplet.get("direct_baseline", {})
        control = triplet.get("cj_control", {})
        rows.append({
            "study_id": fault.get("study_id", ""),
            "pair_id": pair_id,
            "run_id": fault.get("run_id", ""),
            "framework": fault.get("framework", ""),
            "topology": fault.get("topology", ""),
            "benchmark": fault.get("benchmark", ""),
            "task_id": fault.get("task_id", ""),
            "seed": fault.get("seed", ""),
            "fault_type": fault.get("fault_type", ""),
            "target_role": fault.get("fault_target_role", ""),
            "validity": fault.get("validity", ""),
            "configured": lifecycle.get("configured"),
            "activated": lifecycle.get("activated"),
            "triggered": lifecycle.get("triggered"),
            "manifested": lifecycle.get("manifested"),
            "reverted": lifecycle.get("reverted"),
            "reversion_applicable": fault.get("fault_type") != "role_process_kill",
            "recovered": lifecycle.get("recovered"),
            "fault_event": fault_event,
            "direct_success": direct.get("success"),
            "control_success": control.get("success"),
            "fault_success": fault.get("success"),
            "direct_duration_s": direct.get("duration_s"),
            "control_duration_s": control.get("duration_s"),
            "fault_duration_s": fault.get("duration_s"),
            "direct_scorer_status": direct.get("scorer_status"),
            "control_scorer_status": control.get("scorer_status"),
            "fault_scorer_status": fault.get("scorer_status"),
            "fault_operational_success": fault.get("operational_success"),
            "fault_silent_failure": fault.get("silent_failure"),
            "configured_delay_ms": observed.get("configured_delay_ms"),
            "observed_latency_ms": observed.get("observed_latency_ms"),
            "baseline_latency_ms": observed.get("baseline_latency_ms"),
            "configured_loss_pct": observed.get("configured_loss_pct"),
            "observed_packet_loss_pct": observed.get("observed_packet_loss_pct"),
            "requested_memory_bytes": observed.get("requested_memory_bytes"),
            "memory_delta_bytes": observed.get("memory_delta_bytes"),
            "memory_peak_bytes": observed.get("memory_peak_bytes"),
            "oom_kill_count_before": observed.get("oom_kill_count_before"),
            "oom_kill_count_after": observed.get("oom_kill_count_after"),
            "killed_pid": observed.get("killed_pid"),
            "trigger_error": observed.get("trigger_error"),
            "evidence_reason": observed.get("reason") or lifecycle.get("details"),
        })
    return rows


def write_distributed_infrastructure_outputs(
    records: list[dict[str, Any]],
    results_dir: Path,
) -> None:
    rows = distributed_infrastructure_rows(records)
    csv_path = results_dir / "distributed_infrastructure_runs.csv"
    fields = list(rows[0].keys()) if rows else [
        "study_id", "pair_id", "run_id", "framework", "topology",
        "benchmark", "task_id", "seed", "fault_type", "target_role",
        "validity", "configured", "activated", "triggered", "manifested",
        "reverted", "recovered",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    by_fault: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_fault.setdefault(str(row["fault_type"]), []).append(row)
    summary = {
        "metric_scope": "controlled distributed role-infrastructure campaign",
        "raw_records": len(records),
        "complete_triplets": len(rows),
        "faults": {},
    }
    for name, group in sorted(by_fault.items()):
        count = len(group)
        valid = [row for row in group if row["validity"] == "valid"]
        scored = [
            row for row in valid
            if (
                row["control_scorer_status"] == "ok"
                and row["fault_scorer_status"] == "ok"
                and isinstance(row["control_success"], (int, float))
                and isinstance(row["fault_success"], (int, float))
            )
        ]
        direct_scored = [
            row for row in scored
            if row["direct_scorer_status"] == "ok"
            and isinstance(row["direct_success"], (int, float))
        ]
        baseline_eligible = [
            row for row in direct_scored if row["direct_success"] == 1.0
        ]
        control_eligible = [row for row in scored if row["control_success"] == 1.0]
        baseline_fault_failures = [
            row for row in baseline_eligible if row["fault_success"] == 0.0
        ]
        reversion_applicable = [row for row in group if row["reversion_applicable"]]
        summary["faults"][name] = {
            "configured": count,
            "unique_tasks": len({row["task_id"] for row in group}),
            "valid": len(valid),
            "scored_valid_pairs": len(scored),
            "baseline_eligible_pairs": len(baseline_eligible),
            "control_eligible_pairs": len(control_eligible),
            "activation_rate": sum(row["activated"] is True for row in group) / count,
            "trigger_rate": sum(row["triggered"] is True for row in group) / count,
            "manifestation_rate": sum(row["manifested"] is True for row in group) / count,
            "reversion_rate": (
                sum(row["reverted"] is True for row in reversion_applicable)
                / len(reversion_applicable)
                if reversion_applicable else None
            ),
            "recovery_rate": sum(row["recovered"] is True for row in group) / count,
            "direct_pass_at_1": (
                sum(float(row["direct_success"]) for row in direct_scored) / len(direct_scored)
                if direct_scored else None
            ),
            "control_pass_at_1": (
                sum(float(row["control_success"]) for row in scored) / len(scored)
                if scored else None
            ),
            "fault_pass_at_1": (
                sum(float(row["fault_success"]) for row in scored) / len(scored)
                if scored else None
            ),
            "fault_specific_degradation": (
                sum(float(row["control_success"]) - float(row["fault_success"]) for row in scored)
                / len(scored)
                if scored else None
            ),
            "conditional_robustness": (
                sum(float(row["fault_success"]) for row in baseline_eligible)
                / len(baseline_eligible)
                if baseline_eligible else None
            ),
            "silent_failure_rate": (
                sum(row["fault_silent_failure"] is True for row in baseline_fault_failures)
                / len(baseline_fault_failures)
                if baseline_fault_failures else None
            ),
        }
    (results_dir / "distributed_infrastructure_summary.json").write_text(
        json.dumps(redact_secrets(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _record_from_result(
    *,
    result: dict[str, Any],
    task: Any,
    task_data: dict[str, Any],
    study_id: str,
    campaign_id: str,
    pair_id: str,
    run_id: str,
    seed: int,
    model: str,
    docker_image: str,
    topology: str,
    condition: str,
    fault_name: str,
    fault_parameters: dict[str, Any],
    agent_system: str,
    task_selection_seed: int,
) -> dict[str, Any]:
    definition = default_experiment_definition(fault_name)
    lifecycle = (
        result.get("distributed_fault_lifecycle")
        if condition == "cj_fault"
        else {"configured": False, "verdict": "valid"}
    )
    validity = (
        str((lifecycle or {}).get("verdict") or "inconclusive").lower()
        if condition == "cj_fault"
        else "valid"
    )
    fault_plan = DistributedFaultPlan.from_config(fault_parameters)
    labels = distributed_agent_labels(agent_system)
    if fault_name in CONTROLLED_INFRASTRUCTURE_FAULTS:
        definition = dataclasses.replace(
            definition,
            blast_radius_boundary=(
                "one disposable role container in a three-container distributed "
                "agent execution; the host and sibling role containers are excluded"
            ),
            expected_activation_evidence=(
                "DockerTarget evidence from the exact role container: tc plus a "
                "gateway probe for network faults, cgroup memory measurements for "
                "memory pressure, or an observed distributed-entrypoint PID transition"
            ),
            recovery_condition=(
                "Network qdisc or stress process removal is verified; for irreversible "
                "process termination, the role container must accept a fresh process"
            ),
        )
    record = {
        **result,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "study_id": study_id,
        "campaign_id": campaign_id,
        "pair_id": pair_id,
        "run_id": run_id,
        "condition": condition,
        "phase": "fault" if condition == "cj_fault" else "baseline",
        "agent_level": "multi_agent",
        **labels,
        "deployment_mode": "distributed",
        "topology": topology,
        "benchmark": task_data.get("benchmark", ""),
        "task_id": task_data.get("task_id", ""),
        "seed": seed,
        "task_selection_seed": task_selection_seed,
        "model": model,
        "endpoint_type": (
            "distributed_reference_direct"
            if agent_system == REFERENCE_DISTRIBUTED_SYSTEM and condition == "direct_baseline"
            else "distributed_reference_control"
            if agent_system == REFERENCE_DISTRIBUTED_SYSTEM and condition == "cj_control"
            else "distributed_reference_fault"
            if agent_system == REFERENCE_DISTRIBUTED_SYSTEM
            else "distributed_native_direct"
            if condition == "direct_baseline"
            else "distributed_native_control"
            if condition == "cj_control"
            else "distributed_native_fault"
        ),
        "fault_type": fault_name,
        "fault_layer": fault_plan.layer(),
        "fault_parameters": fault_parameters,
        "fault_target_role": fault_parameters.get("target_role", ""),
        "target": "container_per_role",
        "validity": validity,
        "lifecycle": lifecycle,
        "distributed_fault_evidence": result.get("distributed_fault_evidence", []),
        "experiment_definition": definition.to_dict(),
        "cj_commit": _CJ_COMMIT,
        **_CJ_SOURCE_PROVENANCE,
        "docker_image": docker_image,
        "artifact_path": result.get("artifact_paths", {}),
    }
    record.update(_task_complexity_fields(task))
    return record


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _validate_distributed_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    model_cfg = _publication_model_config(args, None)
    if args.container_base_url:
        model_cfg["base_url"] = args.container_base_url
    elif args.base_url:
        model_cfg["base_url"] = args.base_url
    if model_cfg.get("base_url"):
        os.environ["CJ_EVAL_BASE_URL"] = str(model_cfg["base_url"])
    if model_cfg.get("name"):
        os.environ["CJ_EVAL_MODEL"] = str(model_cfg["name"])

    tasks = _load_task_subset(
        args.benchmark,
        args.tasks,
        args.task_selection_seed,
        benchmark_subset=args.benchmark_subset,
        complexity_balanced=True,
    )
    study_id = args.study_id or f"dist-study-{uuid.uuid4().hex[:12]}"
    campaign_id = f"dist-{args.topology}-{args.benchmark}-{uuid.uuid4().hex[:8]}"

    fault_names = _distributed_fault_names(args)
    capabilities = _required_capabilities(fault_names)
    docker_cfg = DockerExecutionConfig(
        image=args.docker_image,
        cpus=args.cpus,
        memory=args.memory,
        timeout_s=args.container_timeout,
        env_file=os.path.abspath(args.env_file) if args.env_file else None,
        preserve_io=True,
        capabilities=capabilities,
    )
    runner = (
        DistributedReferenceRunner(docker_cfg)
        if args.agent_system == REFERENCE_DISTRIBUTED_SYSTEM
        else DistributedNativeRunner(docker_cfg, native_system=args.agent_system)
    )
    proxy_helpers = PublicationStudyOrchestrator(
        runner.docker_runner,
        study_id=study_id,
        results_dir=str(results_dir),
        proxy_port=args.proxy_port,
    )
    proxy_db = SessionDB(path=str(results_dir / "cj_proxy_sessions.sqlite3"))
    base_execution_cfg = {
        "dry_run": args.dry_run,
        "max_turns": args.max_turns,
        "score_timeout_s": args.score_timeout,
        "agent_level": "multi_agent",
        "max_revisions": 2,
        "distributed_agent_system": args.agent_system,
    }

    if not args.triplet and fault_names != ["none"]:
        args.triplet = True

    print("\n[cj-eval] === Distributed multi-agent smoke ===")
    print(f"  study_id  : {study_id}")
    print(f"  image     : {args.docker_image}")
    print(f"  system    : {args.agent_system}")
    print(f"  topology  : {args.topology}")
    print(f"  benchmark : {args.benchmark} ({len(tasks)} tasks, subset={args.benchmark_subset})")
    print(f"  task seed : {args.task_selection_seed}")
    print(f"  faults    : {', '.join(fault_names)}")
    print(f"  triplet   : {args.triplet}")
    print(f"  results   : {results_dir}")

    task_manifest = {
        "benchmark": args.benchmark,
        "benchmark_subset": args.benchmark_subset,
        "task_selection_seed": args.task_selection_seed,
        "task_ids": [task.task_id for task in tasks],
        "execution_seed": args.seed,
    }
    (results_dir / "task_manifest.json").write_text(
        json.dumps(task_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    records: list[dict[str, Any]] = []
    for idx, task in enumerate(tasks):
        task_data = benchmark_task_to_dict(task)
        for fault_name in fault_names:
            fault_parameters = _distributed_fault_config(args, fault_name)
            pair_id = make_pair_id(
                study_id=study_id,
                framework=distributed_agent_labels(args.agent_system)["agent_system"],
                topology=args.topology,
                task_id=task.task_id,
                seed=args.seed,
                fault_name=fault_name,
                fault_parameters=fault_parameters,
                repetition=0,
            )
            conditions = (
                ["direct_baseline", "cj_control", "cj_fault"]
                if args.triplet and fault_name not in ("", "none")
                else ["direct_baseline"]
            )
            for condition in conditions:
                run_id = f"dist-{uuid.uuid4().hex[:16]}"
                run_root = results_dir / (
                    f"{idx:03d}-{task.task_id.replace('/', '-')}-{fault_name}-{condition}-{run_id}"
                )
                condition_model_cfg = dict(model_cfg)
                proxy_process = None
                proxy_session_id: int | None = None
                proxy_port: int | None = None
                proxy_recovered = condition == "direct_baseline"
                proxy_evidence = {
                    "intercepted_calls": 0,
                    "affected_calls": 0,
                    "http_status_counts": {},
                }
                endpoint = str(model_cfg.get("base_url") or "")
                if condition in {"cj_control", "cj_fault"} and not args.dry_run:
                    proxy_port = proxy_helpers._select_proxy_ports()[0]  # noqa: SLF001
                    proxy_session_id = proxy_db.open_session(
                        name=f"distributed-{condition}-{pair_id}",
                        target_type="distributed-proxy-control",
                        target_addr=f"127.0.0.1:{proxy_port}",
                    )
                    proxy_process = proxy_helpers._start_passthrough_proxy(  # noqa: SLF001
                        {**model_cfg, "base_url": args.base_url},
                        port=proxy_port,
                        session_id=proxy_session_id,
                        db=proxy_db,
                    )
                    endpoint = proxy_helpers._docker_reachable_proxy_url(proxy_port)  # noqa: SLF001
                    condition_model_cfg["base_url"] = endpoint

                context = RunContext(
                    study_id=study_id,
                    campaign_id=campaign_id,
                    pair_id=pair_id,
                    run_id=run_id,
                    condition=condition,
                    output_root=str(run_root),
                    environment={"CJ_EVAL_BASE_URL": endpoint} if endpoint else {},
                )
                execution_cfg = dict(base_execution_cfg)
                if condition == "cj_fault":
                    execution_cfg["distributed_fault"] = fault_parameters
                try:
                    result = runner.run(
                        topology=args.topology,
                        task=task_data,
                        seed=args.seed,
                        model_config=condition_model_cfg,
                        execution_config=execution_cfg,
                        run_context=context,
                    )
                finally:
                    if proxy_process is not None:
                        proxy_helpers._stop_process(proxy_process)  # noqa: SLF001
                        proxy_helpers._wait_proxy_down(int(proxy_port))  # noqa: SLF001
                        proxy_recovered = True
                    if proxy_session_id is not None:
                        proxy_evidence = _proxy_evidence(
                            proxy_db.get_llm_calls(proxy_session_id)
                        )
                        proxy_db.close_session(
                            proxy_session_id,
                            "reverted" if proxy_recovered else "failed",
                        )
                record = _record_from_result(
                    result=result,
                    task=task,
                    task_data=task_data,
                    study_id=study_id,
                    campaign_id=campaign_id,
                    pair_id=pair_id,
                    run_id=run_id,
                    seed=args.seed,
                    model=str(model_cfg.get("name", args.model)),
                    docker_image=args.docker_image,
                    topology=args.topology,
                    condition=condition,
                    fault_name=fault_name,
                    fault_parameters=fault_parameters,
                    agent_system=args.agent_system,
                    task_selection_seed=args.task_selection_seed,
                )
                if fault_name in COORDINATED_GROUP_FAULTS:
                    definition = dict(record.get("experiment_definition") or {})
                    definition["blast_radius_boundary"] = (
                        "three disposable role containers and one localhost CJ pass-through proxy"
                    )
                    record["experiment_definition"] = definition
                intercepted = int(proxy_evidence.get("intercepted_calls") or 0)
                affected = int(proxy_evidence.get("affected_calls") or 0)
                record.update({
                    "proxy_evidence": proxy_evidence,
                    "proxy_intercepted_calls": intercepted,
                    "proxy_calls_affected": affected,
                    "routing": {
                        "host_upstream_base_url": args.base_url,
                        "container_direct_base_url": args.container_base_url,
                        "container_condition_base_url": endpoint,
                        "proxy_port": proxy_port,
                    },
                })
                if condition == "cj_control":
                    control_valid = (
                        not args.dry_run
                        and intercepted > 0
                        and affected == 0
                        and proxy_recovered
                    )
                    record["validity"] = "valid" if control_valid else "inconclusive"
                    record["lifecycle"] = {
                        "configured": False,
                        "activated": proxy_process is not None,
                        "triggered": intercepted > 0,
                        "manifested": intercepted > 0 and affected == 0,
                        "reverted": proxy_recovered,
                        "recovered": proxy_recovered,
                        "verdict": record["validity"],
                        "evidence_source": "cj_passthrough_proxy_database",
                    }
                elif condition == "cj_fault":
                    proxy_valid = (
                        args.dry_run
                        or (intercepted > 0 and affected == 0 and proxy_recovered)
                    )
                    if not proxy_valid:
                        record["validity"] = "invalid"
                        lifecycle = dict(record.get("lifecycle") or {})
                        lifecycle["verdict"] = "invalid"
                        lifecycle["details"] = (
                            "distributed fault ran without valid CJ pass-through routing"
                        )
                        record["lifecycle"] = lifecycle
                _append_jsonl(results_dir / "runs.jsonl", record)
                records.append(record)

    proxy_db.close()
    generate_all_outputs(str(results_dir), study_id=study_id)
    if any(name in COORDINATED_GROUP_FAULTS for name in fault_names):
        write_distributed_coordination_outputs(records, results_dir)
    if any(name in CONTROLLED_INFRASTRUCTURE_FAULTS for name in fault_names):
        write_distributed_infrastructure_outputs(records, results_dir)
    print(f"[cj-eval] distributed smoke complete: {results_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
