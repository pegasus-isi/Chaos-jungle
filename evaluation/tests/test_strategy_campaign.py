from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from evaluation.strategy_campaign import (
    IMPLEMENTED_STRATEGIES,
    UNSUPPORTED_STRATEGIES,
    _random_seed,
    _write_summary,
    wilson_interval,
)


def _record(strategy: str, validity: str = "valid") -> dict:
    return {
        "strategy": strategy,
        "validity": validity,
        "lifecycle": {
            "triggered": True,
            "manifested": validity == "valid",
            "recovered": True,
        },
        "metrics": (
            {"absolute_calibration_error": 0.01}
            if strategy == "percentage_stochastic"
            else {}
        ),
    }


def test_wilson_interval_contains_observed_fraction_and_validates_inputs():
    low, high = wilson_interval(25, 100)
    assert low < 0.25 < high
    assert wilson_interval(0, 100)[0] == 0.0
    assert wilson_interval(100, 100)[1] == 1.0
    with pytest.raises(ValueError):
        wilson_interval(0, 0)
    with pytest.raises(ValueError):
        wilson_interval(11, 10)


def test_seed_scope_is_reproducible_without_changing_caller_random_state():
    random.seed(8675309)
    before = random.getstate()
    with _random_seed(11):
        first = [random.random() for _ in range(10)]
    assert random.getstate() == before
    with _random_seed(11):
        second = [random.random() for _ in range(10)]
    assert first == second
    assert random.getstate() == before


def test_strategy_catalog_is_honest_about_partial_and_planned_capabilities():
    assert IMPLEMENTED_STRATEGIES["percentage_stochastic"] == "validated"
    assert IMPLEMENTED_STRATEGIES["scheduled_interval"] == "validated_partial"
    assert "event_driven" in UNSUPPORTED_STRATEGIES
    assert "cascading" in UNSUPPORTED_STRATEGIES
    assert "targeted_header" in UNSUPPORTED_STRATEGIES


def test_summary_counts_validity_and_keeps_unsupported_catalog(tmp_path):
    records = [
        _record("manual_on_demand"),
        _record("percentage_stochastic"),
        _record("percentage_stochastic", "invalid"),
    ]
    summary = _write_summary(tmp_path, "strategy-study-test", records)

    assert summary["records"] == 3
    assert summary["valid_records"] == 2
    assert summary["invalid_records"] == 1
    assert summary["mean_probability_calibration_error"] == pytest.approx(0.01)
    stored = json.loads((tmp_path / "strategy_fidelity_summary.json").read_text())
    assert stored["unsupported_strategy_status"]["adaptive"]
    csv_text = (tmp_path / "strategy_summary.csv").read_text()
    assert "percentage_stochastic,2,1,1" in csv_text


def test_strategy_launcher_is_executable_and_bundles_preserve_infrastructure():
    repo = Path(__file__).resolve().parents[2]
    launcher = repo / "evaluation/scripts/run_strategy_evaluation.sh"
    assert launcher.exists()
    assert launcher.stat().st_mode & 0o111

    all_current = (repo / "evaluation/scripts/run_all_current_scenarios.sh").read_text()
    assert "run_strategy_evaluation.sh" in all_current
    assert "run_infrastructure_fault_smoke.sh" in all_current
