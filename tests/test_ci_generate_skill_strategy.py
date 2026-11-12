"""Offline checks for the GitLab skill-guided strategy generator."""

import json
from io import BytesIO
import subprocess
import sys
from pathlib import Path

import pytest

from ci.generate_skill_strategy import (
    SKILL_FILES,
    build_messages,
    discover_repository,
    main,
    model_config,
    parse_strategy,
    request_strategy,
    render_markdown,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _case(case_id: str, path: str) -> dict:
    return {
        "id": case_id,
        "objective": "Verify bounded fault exposure",
        "source_paths": [path],
        "workload": "One scheduling task",
        "fault": "Selected LLM call unavailable",
        "injection_boundary": "Model request boundary",
        "controls": "Direct and CJ pass-through",
        "outcome_metrics": ["jobs completed"],
        "exposure_evidence": ["matched request", "manifested response"],
        "recovery_check": "Healthy call after stop",
        "safety": "One disposable worker, short timeout",
        "artifacts": ["run configuration", "CJ evidence"],
    }


def test_discovery_uses_tracked_source_and_excludes_secret_or_bulk_dirs(tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ci@example.invalid")
    _git(repo, "config", "user.name", "CI")
    (repo / "README.md").write_text("# SWARM+\nA scheduling system.\n")
    (repo / "run_test.py").write_text("def run_scheduling():\n    pass\n")
    (repo / ".secrets").mkdir()
    (repo / ".secrets" / "api_key.md").write_text("must-not-appear")
    (repo / "runs").mkdir()
    (repo / "runs" / "result.md").write_text("bulk-result")
    external = tmp_path / "outside.md"
    external.write_text("external-private-content")
    (repo / "linked.md").symlink_to(external)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "fixture")

    inventory = discover_repository(repo)
    assert inventory["source_paths"] == ["README.md", "run_test.py", "linked.md"]
    assert "run_scheduling" in inventory["summaries"]["run_test.py"]
    assert "must-not-appear" not in json.dumps(inventory)
    assert "external-private-content" not in json.dumps(inventory)


def test_model_config_requires_https_v1_without_embedded_credentials():
    env = {
        "CJ_EVAL_BASE_URL": "https://ellm.example.test/v1",
        "CJ_EVAL_MODEL": "example-model",
        "CJ_EVAL_API_KEY": "private",
    }
    assert model_config(env) == ("https://ellm.example.test/v1", "example-model", "private")
    with pytest.raises(ValueError, match="HTTPS"):
        model_config({**env, "CJ_EVAL_BASE_URL": "http://ellm.example.test/v1"})
    with pytest.raises(ValueError, match="HTTPS"):
        model_config({**env, "CJ_EVAL_BASE_URL": "https://user:pass@ellm.example.test/v1"})


def test_strategy_validation_and_markdown():
    inventory = {"revision": "abc123", "source_paths": ["run_test.py"], "summaries": {}}
    strategy = {
        "system_under_test": "SWARM+ scheduling",
        "cases": [_case("one", "run_test.py"), _case("two", "run_test.py"),
                  _case("three", "run_test.py")],
        "execution_order": ["healthy control", "fault"],
        "unknowns": ["Live routing not yet verified"],
        "artifact_contract": ["session DB", "score table"],
    }
    parsed = parse_strategy("```json\n" + json.dumps(strategy) + "\n```", inventory)
    report = render_markdown(parsed, inventory)
    assert "Target revision: `abc123`" in report
    assert "not an executed experiment" in report
    assert "Live routing not yet verified" in report

    strategy["cases"][0]["source_paths"] = ["not-in-repo.py"]
    with pytest.raises(ValueError, match="outside the inventory"):
        parse_strategy(json.dumps(strategy), inventory)


def test_skill_content_is_passed_as_instructions_not_repo_commands():
    messages = build_messages(
        {"revision": "abc", "source_paths": ["README.md"], "summaries": {}},
        {"chaos-jungle-evaluation/SKILL.md": "evaluation guidance"},
        "CJ exports",
    )
    assert "untrusted evidence" in messages[0]["content"]
    assert "evaluation guidance" in messages[1]["content"]
    assert "CJ exports" in messages[1]["content"]


def test_strategy_request_uses_bounded_output_and_timeout(monkeypatch):
    observed = {}

    def fake_urlopen(request, timeout):
        observed["timeout"] = timeout
        observed["payload"] = json.loads(request.data)
        return BytesIO(b'{"choices": [{"message": {"content": "{}"}}]}')

    monkeypatch.setattr("ci.generate_skill_strategy.urlopen", fake_urlopen)
    assert request_strategy(
        "https://example.invalid/v1", "model", "key", [], reasoning_effort="low"
    ) == "{}"
    assert observed["timeout"] == 300
    assert observed["payload"]["max_tokens"] == 8192
    assert observed["payload"]["reasoning_effort"] == "low"


def test_strategy_request_reports_timeout_without_credentials(monkeypatch):
    def fake_urlopen(request, timeout):
        raise TimeoutError("socket read timed out")

    monkeypatch.setattr("ci.generate_skill_strategy.urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="did not respond within 300 seconds") as error:
        request_strategy("https://example.invalid/v1", "model", "private-key", [])
    assert "private-key" not in str(error.value)


def test_strategy_request_reports_empty_response_metadata(monkeypatch):
    def fake_urlopen(request, timeout):
        return BytesIO(json.dumps({
            "choices": [{"finish_reason": "length", "message": {"content": ""}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 8192, "total_tokens": 8292},
        }).encode())

    monkeypatch.setattr("ci.generate_skill_strategy.urlopen", fake_urlopen)
    with pytest.raises(
        ValueError, match="finish_reason='length', prompt_tokens=100, completion_tokens=8192"
    ):
        request_strategy("https://example.invalid/v1", "model", "private-key", [])


def test_main_writes_reviewable_artifacts_without_a_live_model(tmp_path, monkeypatch):
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "ci@example.invalid")
    _git(repo, "config", "user.name", "CI")
    (repo / "README.md").write_text("# SWARM+\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "fixture")
    skills = tmp_path / "skills"
    for name in SKILL_FILES:
        path = skills / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Skill instructions")
    cj_root = tmp_path / "cj"
    (cj_root / "chaos_jungle").mkdir(parents=True)
    (cj_root / "chaos_jungle" / "__init__.py").write_text("# CJ exports")
    strategy = {
        "system_under_test": "SWARM+",
        "cases": [_case("one", "README.md"), _case("two", "README.md"),
                  _case("three", "README.md")],
        "execution_order": ["pilot", "review"],
        "unknowns": [],
        "artifact_contract": ["run manifest"],
    }
    monkeypatch.setattr(
        "ci.generate_skill_strategy.model_config",
        lambda _: ("https://example.invalid/v1", "test-model", "test-key"),
    )
    monkeypatch.setattr("ci.generate_skill_strategy.request_strategy",
                        lambda *args, **kwargs: json.dumps(strategy))
    output = tmp_path / "output"
    monkeypatch.setattr(sys, "argv", [
        "generate_skill_strategy.py", "--repo", str(repo), "--skills-dir", str(skills),
        "--cj-root", str(cj_root), "--output-dir", str(output),
    ])
    main()
    assert (output / "strategy.md").read_text().startswith("# Skill-guided CJ test strategy")
    assert json.loads((output / "generation.json").read_text())["case_count"] == 3
    assert json.loads((output / "repository-inventory.json").read_text())["working_tree_clean"]
