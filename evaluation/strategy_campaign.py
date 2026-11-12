"""E10: empirical validation of Chaos Jungle injection strategies.

This campaign is intentionally independent from the agent-resilience and
container-infrastructure studies.  It exercises the public in-process HTTP
interception APIs against a real loopback HTTP server, so the observations are
cheap, reproducible, and attributable to strategy semantics rather than model
variance.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import random
import statistics
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import httpx

from chaos_jungle.core.scenario import Scenario
from chaos_jungle.core.scheduler import ChaosScheduler
from chaos_jungle.inject.intercept import Unavailable, door, inject
from evaluation.docker_runner import redact_secrets
from evaluation.experiments.protocol import _CJ_COMMIT, _CJ_SOURCE_PROVENANCE
from evaluation.schema import OUTPUT_SCHEMA_VERSION


IMPLEMENTED_STRATEGIES = {
    "manual_on_demand": "validated",
    "percentage_stochastic": "validated",
    "ramp_up_progressive": "validated",
    "cycling_door": "validated",
    "targeted_url": "validated_partial",
    "targeted_model": "validated_partial",
    "targeted_tool": "validated_partial",
    "delayed_call_trigger": "validated_partial",
    "scheduled_interval": "validated_partial",
}

UNSUPPORTED_STRATEGIES = {
    "event_driven": "documented as planned; no public trigger API",
    "time_window": "documented as planned; daily scheduling is not a bounded active window",
    "burst": "documented as planned; no burst controller API",
    "canary": "documented as planned; probability is not labeled as canary routing",
    "correlated": "documented as planned; no shared-state correlation API",
    "adaptive": "documented as planned; no health-feedback controller API",
    "cascading": "documented as partial but no explicit causal chaining API is validated here",
    "targeted_header": "documentation mentions header matching, but inject() exposes URL/model/tool filters",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(redact_secrets(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _append_jsonl(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(redact_secrets(payload), sort_keys=True) + "\n")


def wilson_interval(successes: int, trials: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Return a two-sided Wilson score interval for a binomial proportion."""
    if trials <= 0:
        raise ValueError("trials must be positive")
    if successes < 0 or successes > trials:
        raise ValueError("successes must be between zero and trials")
    proportion = successes / trials
    z2 = z * z
    denominator = 1.0 + z2 / trials
    centre = (proportion + z2 / (2.0 * trials)) / denominator
    margin = z * math.sqrt(
        (proportion * (1.0 - proportion) + z2 / (4.0 * trials)) / trials
    ) / denominator
    low = 0.0 if successes == 0 else max(0.0, centre - margin)
    high = 1.0 if successes == trials else min(1.0, centre + margin)
    return low, high


@contextmanager
def _random_seed(seed: int) -> Iterator[None]:
    state = random.getstate()
    random.seed(seed)
    try:
        yield
    finally:
        random.setstate(state)


class _ProbeHandler(BaseHTTPRequestHandler):
    # HTTP/1.0 avoids delayed-ACK behaviour on tiny persistent loopback
    # responses, keeping the paper profile bounded while retaining real sockets.
    protocol_version = "HTTP/1.0"

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(length)
        body = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _respond
    do_POST = _respond

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


@contextmanager
def _probe_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _statuses(
    client: httpx.Client,
    url: str,
    count: int,
    *,
    payload: dict[str, Any] | None = None,
) -> list[int]:
    return [client.post(url, json=payload or {"model": "probe"}).status_code for _ in range(count)]


def _lifecycle(
    *,
    triggered: bool,
    manifested: bool,
    recovered: bool,
    validity: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "configured": True,
        "activated": True,
        "triggered": triggered,
        "manifested": manifested,
        "reverted": True,
        "recovered": recovered,
        "validity": validity,
        "evidence_source": "loopback_http_observation",
        "details": details or {},
    }


def _record(
    *,
    study_id: str,
    strategy: str,
    trial: str,
    parameters: dict[str, Any],
    metrics: dict[str, Any],
    lifecycle: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "study": "E10-strategy-fidelity",
        "study_id": study_id,
        "run_id": f"strategy-run-{uuid.uuid4().hex[:12]}",
        "timestamp": _now(),
        "strategy": strategy,
        "trial": trial,
        "parameters": parameters,
        "metrics": metrics,
        "lifecycle": lifecycle,
        "validity": lifecycle["validity"],
        "cj_commit": _CJ_COMMIT,
        **_CJ_SOURCE_PROVENANCE,
    }


def _probability_trial(
    client: httpx.Client,
    url: str,
    *,
    probability: float,
    requests: int,
    seed: int,
) -> tuple[list[int], dict[str, Any]]:
    with _random_seed(seed):
        with inject(Unavailable(probability=probability), urls=[url]):
            statuses = _statuses(client, url, requests)
    affected = statuses.count(503)
    low, high = wilson_interval(affected, requests)
    return statuses, {
        "requests": requests,
        "affected_requests": affected,
        "observed_fraction": affected / requests,
        "absolute_calibration_error": abs(affected / requests - probability),
        "wilson_ci95_low": low,
        "wilson_ci95_high": high,
        "configured_probability_in_ci95": low <= probability <= high,
    }


def _run_manual(client: httpx.Client, base_url: str, study_id: str, requests: int) -> dict[str, Any]:
    url = f"{base_url}/manual"
    before = client.get(url).status_code
    with inject(Unavailable(), urls=[url]):
        fault_statuses = _statuses(client, url, requests)
    after = client.get(url).status_code
    manifested = all(status == 503 for status in fault_statuses)
    recovered = before == 200 and after == 200
    validity = "valid" if manifested and recovered else "invalid"
    return _record(
        study_id=study_id,
        strategy="manual_on_demand",
        trial="explicit_context_scope",
        parameters={"requests": requests, "fault": "Unavailable"},
        metrics={
            "before_status": before,
            "fault_status_counts": {str(code): fault_statuses.count(code) for code in sorted(set(fault_statuses))},
            "after_status": after,
            "scope_leakage_calls": int(before != 200) + int(after != 200),
        },
        lifecycle=_lifecycle(
            triggered=bool(fault_statuses),
            manifested=manifested,
            recovered=recovered,
            validity=validity,
        ),
    )


def _run_probability(
    client: httpx.Client,
    base_url: str,
    study_id: str,
    requests: int,
    seeds: list[int],
    probabilities: list[float],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    url = f"{base_url}/stochastic"
    for seed in seeds:
        for probability in probabilities:
            statuses, metrics = _probability_trial(
                client,
                url,
                probability=probability,
                requests=requests,
                seed=seed,
            )
            replay, _ = _probability_trial(
                client,
                url,
                probability=probability,
                requests=requests,
                seed=seed,
            )
            metrics["deterministic_replay"] = replay == statuses
            metrics["sequence_sha256"] = hashlib.sha256(
                json.dumps(statuses).encode()
            ).hexdigest()
            recovered = client.get(url).status_code == 200
            manifested = metrics["affected_requests"] > 0 if probability > 0 else metrics["affected_requests"] == 0
            valid = bool(metrics["configured_probability_in_ci95"] and metrics["deterministic_replay"] and recovered)
            records.append(_record(
                study_id=study_id,
                strategy="percentage_stochastic",
                trial=f"seed={seed},p={probability:g}",
                parameters={"seed": seed, "probability": probability, "requests": requests},
                metrics=metrics,
                lifecycle=_lifecycle(
                    triggered=metrics["affected_requests"] > 0,
                    manifested=manifested,
                    recovered=recovered,
                    validity="valid" if valid else "invalid",
                ),
            ))
    return records


def _run_progressive(
    client: httpx.Client,
    base_url: str,
    study_id: str,
    requests: int,
    seeds: list[int],
    probabilities: list[float],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    url = f"{base_url}/ramp"
    for seed in seeds:
        steps: list[dict[str, Any]] = []
        for probability in probabilities:
            _, metrics = _probability_trial(
                client,
                url,
                probability=probability,
                requests=requests,
                seed=seed,
            )
            steps.append({"probability": probability, **metrics})
        fractions = [float(step["observed_fraction"]) for step in steps]
        monotonic = all(a <= b for a, b in zip(fractions, fractions[1:]))
        tipping = next(
            (step["probability"] for step in steps if step["observed_fraction"] >= 0.5),
            None,
        )
        endpoint_ok = (
            (probabilities[0] != 0.0 or fractions[0] == 0.0)
            and (probabilities[-1] != 1.0 or fractions[-1] == 1.0)
        )
        recovered = client.get(url).status_code == 200
        valid = monotonic and endpoint_ok and recovered
        records.append(_record(
            study_id=study_id,
            strategy="ramp_up_progressive",
            trial=f"seed={seed}",
            parameters={"seed": seed, "probabilities": probabilities, "requests_per_step": requests},
            metrics={
                "steps": steps,
                "observed_fractions": fractions,
                "monotonic_non_decreasing": monotonic,
                "monotonic_inversions": sum(a > b for a, b in zip(fractions, fractions[1:])),
                "observed_tipping_probability": tipping,
            },
            lifecycle=_lifecycle(
                triggered=any(value > 0 for value in fractions),
                manifested=monotonic and endpoint_ok,
                recovered=recovered,
                validity="valid" if valid else "invalid",
            ),
        ))
    return records


def _run_selector(
    client: httpx.Client,
    base_url: str,
    study_id: str,
    requests: int,
    selector: str,
) -> dict[str, Any]:
    target_url = f"{base_url}/selector-target"
    control_url = f"{base_url}/selector-control"
    target_statuses: list[int] = []
    control_statuses: list[int] = []

    kwargs: dict[str, Any] = {}
    urls = [base_url]
    target_payload: dict[str, Any] = {"model": "target-model"}
    control_payload: dict[str, Any] = {"model": "control-model"}
    if selector == "url":
        urls = [target_url]
    elif selector == "model":
        kwargs["only_model"] = "target-model"
    elif selector == "tool":
        kwargs["only_tool"] = "target-tool"
        target_payload = {
            "model": "probe",
            "messages": [{"role": "tool", "name": "target-tool", "content": "ok"}],
        }
        control_payload = {
            "model": "probe",
            "messages": [{"role": "tool", "name": "control-tool", "content": "ok"}],
        }
    else:  # pragma: no cover - internal caller is fixed
        raise ValueError(f"unsupported selector: {selector}")

    with inject(Unavailable(), urls=urls, **kwargs):
        for _ in range(requests):
            target_statuses.extend(_statuses(client, target_url, 1, payload=target_payload))
            control_statuses.extend(_statuses(client, control_url, 1, payload=control_payload))

    true_positive = target_statuses.count(503)
    false_positive = control_statuses.count(503)
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else None
    coverage = true_positive / requests
    spillover = false_positive / requests
    recovered = client.get(target_url).status_code == 200 and client.get(control_url).status_code == 200
    valid = precision == 1.0 and coverage == 1.0 and spillover == 0.0 and recovered
    return _record(
        study_id=study_id,
        strategy=f"targeted_{selector}",
        trial=f"{selector}_selector",
        parameters={"selector": selector, "target_requests": requests, "control_requests": requests},
        metrics={
            "target_affected_calls": true_positive,
            "control_affected_calls": false_positive,
            "selectivity_precision": precision,
            "target_coverage": coverage,
            "spillover_rate": spillover,
        },
        lifecycle=_lifecycle(
            triggered=true_positive > 0,
            manifested=valid,
            recovered=recovered,
            validity="valid" if valid else "invalid",
        ),
    )


def _run_delayed_trigger(
    client: httpx.Client,
    base_url: str,
    study_id: str,
    requests: int,
    after_n_calls: int,
) -> dict[str, Any]:
    url = f"{base_url}/delayed"
    with inject(Unavailable(), urls=[url], after_n_calls=after_n_calls):
        statuses = _statuses(client, url, requests)
    affected_indices = [index + 1 for index, status in enumerate(statuses) if status == 503]
    observed_trigger_index = affected_indices[0] if affected_indices else None
    expected_trigger_index = after_n_calls + 1
    exact = observed_trigger_index == expected_trigger_index
    prefix_clean = all(status == 200 for status in statuses[:after_n_calls])
    suffix_affected = all(status == 503 for status in statuses[after_n_calls:])
    recovered = client.get(url).status_code == 200
    valid = exact and prefix_clean and suffix_affected and recovered
    return _record(
        study_id=study_id,
        strategy="delayed_call_trigger",
        trial=f"after_n_calls={after_n_calls}",
        parameters={"requests": requests, "after_n_calls": after_n_calls},
        metrics={
            "status_sequence": statuses,
            "expected_trigger_index": expected_trigger_index,
            "observed_trigger_index": observed_trigger_index,
            "trigger_index_error": None if observed_trigger_index is None else observed_trigger_index - expected_trigger_index,
            "prefix_clean": prefix_clean,
            "suffix_affected": suffix_affected,
        },
        lifecycle=_lifecycle(
            triggered=observed_trigger_index is not None,
            manifested=exact and prefix_clean and suffix_affected,
            recovered=recovered,
            validity="valid" if valid else "invalid",
        ),
    )


def _run_cycling(
    client: httpx.Client,
    base_url: str,
    study_id: str,
    cycles: int,
    phase_duration_s: float,
) -> dict[str, Any]:
    url = f"{base_url}/cycling"

    def workload() -> dict[str, Any]:
        started = time.monotonic()
        status = client.get(url).status_code
        return {"status": status, "duration_s": time.monotonic() - started}

    started = time.monotonic()
    phases = door(
        Unavailable(),
        fault_duration=phase_duration_s,
        rest_duration=phase_duration_s,
        cycles=cycles,
        workload=workload,
        urls=[url],
    )
    elapsed = time.monotonic() - started
    expected_phase_count = cycles * 2
    correct_phases = sum(
        (phase["phase"] == "fault" and phase["metrics"].get("status") == 503)
        or (phase["phase"] == "rest" and phase["metrics"].get("status") == 200)
        for phase in phases
    )
    recovered = client.get(url).status_code == 200
    valid = len(phases) == expected_phase_count and correct_phases == expected_phase_count and recovered
    return _record(
        study_id=study_id,
        strategy="cycling_door",
        trial=f"cycles={cycles}",
        parameters={"cycles": cycles, "fault_duration_s": phase_duration_s, "rest_duration_s": phase_duration_s},
        metrics={
            "phase_count": len(phases),
            "expected_phase_count": expected_phase_count,
            "correct_phase_count": correct_phases,
            "phase_accuracy": correct_phases / expected_phase_count,
            "elapsed_s": elapsed,
            "phases": phases,
        },
        lifecycle=_lifecycle(
            triggered=any(phase["metrics"].get("status") == 503 for phase in phases),
            manifested=correct_phases == expected_phase_count,
            recovered=recovered,
            validity="valid" if valid else "invalid",
        ),
    )


def _run_scheduler(
    study_id: str,
    interval_s: float,
    observation_window_s: float,
) -> dict[str, Any]:
    timestamps: list[float] = []
    lock = threading.Lock()
    scheduler = ChaosScheduler(Scenario("strategy-scheduler-probe", faults=[])).every(interval_s)

    def tick() -> None:
        with lock:
            timestamps.append(time.monotonic())

    # The scheduler strategy itself is under test.  Replacing the workload body
    # avoids host fault execution and isolates interval dispatch semantics.
    scheduler._run_once = tick  # type: ignore[method-assign]
    started = time.monotonic()
    scheduler.start()
    time.sleep(observation_window_s)
    scheduler.stop()
    stopped_count = len(timestamps)
    time.sleep(interval_s * 1.5)
    stopped_cleanly = len(timestamps) == stopped_count
    offsets = [stamp - started for stamp in timestamps]
    intervals = [b - a for a, b in zip(timestamps, timestamps[1:])]
    jitter = [abs(value - interval_s) for value in intervals]
    minimum_expected = max(2, math.floor(observation_window_s / interval_s) - 1)
    valid = stopped_count >= minimum_expected and stopped_cleanly and all(value > 0 for value in intervals)
    return _record(
        study_id=study_id,
        strategy="scheduled_interval",
        trial=f"interval={interval_s:g}",
        parameters={"interval_s": interval_s, "observation_window_s": observation_window_s},
        metrics={
            "tick_count": stopped_count,
            "minimum_expected_ticks": minimum_expected,
            "tick_offsets_s": offsets,
            "observed_intervals_s": intervals,
            "median_interval_s": statistics.median(intervals) if intervals else None,
            "max_absolute_jitter_s": max(jitter) if jitter else None,
            "stopped_cleanly": stopped_cleanly,
        },
        lifecycle=_lifecycle(
            triggered=stopped_count > 0,
            manifested=stopped_count >= minimum_expected,
            recovered=stopped_cleanly,
            validity="valid" if valid else "invalid",
            details={"scope": "scheduler dispatch timing; no host fault was applied"},
        ),
    )


def _write_summary(results: Path, study_id: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for strategy in sorted({record["strategy"] for record in records}):
        subset = [record for record in records if record["strategy"] == strategy]
        rows.append({
            "study_id": study_id,
            "strategy": strategy,
            "trials": len(subset),
            "valid_trials": sum(record["validity"] == "valid" for record in subset),
            "invalid_trials": sum(record["validity"] == "invalid" for record in subset),
            "triggered_trials": sum(bool(record["lifecycle"]["triggered"]) for record in subset),
            "manifested_trials": sum(bool(record["lifecycle"]["manifested"]) for record in subset),
            "recovered_trials": sum(bool(record["lifecycle"]["recovered"]) for record in subset),
            "validity_rate": sum(record["validity"] == "valid" for record in subset) / len(subset),
        })
    path = results / "strategy_summary.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    stochastic = [record for record in records if record["strategy"] == "percentage_stochastic"]
    summary = {
        "study": "E10-strategy-fidelity",
        "study_id": study_id,
        "records": len(records),
        "strategies_exercised": len(rows),
        "valid_records": sum(record["validity"] == "valid" for record in records),
        "invalid_records": sum(record["validity"] == "invalid" for record in records),
        "all_exercised_strategies_valid": all(row["invalid_trials"] == 0 for row in rows),
        "mean_probability_calibration_error": (
            statistics.mean(record["metrics"]["absolute_calibration_error"] for record in stochastic)
            if stochastic else None
        ),
        "max_probability_calibration_error": (
            max(record["metrics"]["absolute_calibration_error"] for record in stochastic)
            if stochastic else None
        ),
        "implemented_strategy_status": IMPLEMENTED_STRATEGIES,
        "unsupported_strategy_status": UNSUPPORTED_STRATEGIES,
    }
    _write_json(results / "strategy_fidelity_summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--requests-per-probability", type=int, default=500)
    parser.add_argument("--selector-requests", type=int, default=100)
    parser.add_argument("--seeds", default="11,29,43")
    parser.add_argument("--probabilities", default="0,0.1,0.25,0.5,0.75,1")
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--phase-duration-s", type=float, default=0.01)
    parser.add_argument("--after-n-calls", type=int, default=3)
    parser.add_argument("--scheduler-interval-s", type=float, default=0.05)
    parser.add_argument("--scheduler-window-s", type=float, default=0.35)
    return parser


def run(args: argparse.Namespace) -> int:
    if args.requests_per_probability < 20:
        raise SystemExit("--requests-per-probability must be at least 20")
    if args.selector_requests < 1 or args.cycles < 1 or args.after_n_calls < 0:
        raise SystemExit("selector requests/cycles must be positive and after-n-calls non-negative")
    seeds = [int(part.strip()) for part in args.seeds.split(",") if part.strip()]
    probabilities = [float(part.strip()) for part in args.probabilities.split(",") if part.strip()]
    if not seeds or not probabilities or any(value < 0 or value > 1 for value in probabilities):
        raise SystemExit("seeds must be non-empty and probabilities must be in [0, 1]")
    probabilities = sorted(set(probabilities))

    results = Path(args.results_dir).resolve()
    results.mkdir(parents=True, exist_ok=True)
    study_id = f"strategy-study-{uuid.uuid4().hex[:12]}"
    started_at = _now()
    config = {
        "requests_per_probability": args.requests_per_probability,
        "selector_requests": args.selector_requests,
        "seeds": seeds,
        "probabilities": probabilities,
        "cycles": args.cycles,
        "phase_duration_s": args.phase_duration_s,
        "after_n_calls": args.after_n_calls,
        "scheduler_interval_s": args.scheduler_interval_s,
        "scheduler_window_s": args.scheduler_window_s,
    }
    manifest = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "study": "E10-strategy-fidelity",
        "study_id": study_id,
        "started_at": started_at,
        "cj_commit": _CJ_COMMIT,
        **_CJ_SOURCE_PROVENANCE,
        "python_version": platform.python_version(),
        "httpx_version": httpx.__version__,
        "configuration": config,
        "configuration_hash": hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
        "experimental_unit": "one bounded HTTP request sequence against one ephemeral loopback service",
        "paired_agent_conditions": "not_applicable; this is injection-strategy fidelity, not agent resilience",
        "steady_state": "Loopback HTTP probe returns status 200 outside an injection scope.",
        "hypothesis": "Each documented strategy controls fault reach, timing, or intensity according to its configured semantics and restores the steady state after exit.",
        "fault_variable": "HTTP 503 Unavailable response under strategy control.",
        "expected_activation_evidence": "Observed status sequence and strategy-specific counters.",
        "blast_radius": "One in-process httpx client and an ephemeral loopback HTTP server.",
        "abort_policy": "Abort on server failure, invalid configuration, or failed steady-state probe.",
        "observation_window": "Bounded request count or scheduler time window from configuration.",
        "recovery_condition": "Post-strategy loopback request returns HTTP 200 and scheduled ticks stop.",
        "implemented_strategy_status": IMPLEMENTED_STRATEGIES,
        "unsupported_strategy_status": UNSUPPORTED_STRATEGIES,
        "existing_infrastructure_campaign_modified": False,
    }
    _write_json(results / "study_manifest.json", manifest)

    records: list[dict[str, Any]] = []
    with _probe_server() as base_url:
        with httpx.Client(timeout=5.0, trust_env=False) as client:
            if client.get(f"{base_url}/preflight").status_code != 200:
                raise RuntimeError("loopback steady-state preflight failed")
            records.append(_run_manual(client, base_url, study_id, args.selector_requests))
            records.extend(_run_probability(
                client, base_url, study_id, args.requests_per_probability, seeds, probabilities
            ))
            records.extend(_run_progressive(
                client, base_url, study_id, args.requests_per_probability, seeds, probabilities
            ))
            for selector in ("url", "model", "tool"):
                records.append(_run_selector(
                    client, base_url, study_id, args.selector_requests, selector
                ))
            records.append(_run_delayed_trigger(
                client,
                base_url,
                study_id,
                max(args.after_n_calls + 5, 10),
                args.after_n_calls,
            ))
            records.append(_run_cycling(
                client,
                base_url,
                study_id,
                args.cycles,
                args.phase_duration_s,
            ))

    records.append(_run_scheduler(
        study_id,
        args.scheduler_interval_s,
        args.scheduler_window_s,
    ))
    for record in records:
        _append_jsonl(results / "runs.jsonl", record)
    summary = _write_summary(results, study_id, records)
    manifest["finished_at"] = _now()
    manifest["record_count"] = len(records)
    manifest["summary_path"] = "strategy_fidelity_summary.json"
    _write_json(results / "study_manifest.json", manifest)
    print(
        f"[cj-eval] E10 strategy fidelity complete: {summary['valid_records']}/"
        f"{summary['records']} valid records in {results}"
    )
    return 0 if summary["all_exercised_strategies_valid"] else 1


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
