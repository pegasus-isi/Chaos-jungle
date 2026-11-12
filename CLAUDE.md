# Chaos Jungle package release guide

This repository's `main` branch contains the installable `chaos-jungle` package,
documentation, tests, and CI. Paper evaluation code belongs on the separate
`evaluation` branch. Do not add result archives, databases, credentials, or
`.env` files to a package release.

## Local preparation

Use Python 3.10 or newer for release tooling. The published package itself
supports Python 3.9 and newer.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,docs]' build twine
pre-commit install --hook-type pre-commit --hook-type commit-msg
pre-commit run --all-files
python -m pytest tests/ -q --strict-markers
python ci/check_ruff_baseline.py
python -m sphinx -q -b html docs docs/_build/html
```

Commit messages use Conventional Commits, such as `feat: add a fault` or
`fix: restore proxy routing`. The commit-msg hook checks this locally.

## Build and inspect

Always build into a fresh directory; an existing `dist/` may contain an older
version. Build both the wheel and source distribution, then smoke-test the wheel
from an isolated environment:

```sh
DIST_DIR="$(mktemp -d)"
python -m build --outdir "$DIST_DIR"
python -m twine check "$DIST_DIR"/*
python -m venv "$DIST_DIR/venv"
"$DIST_DIR/venv/bin/python" -m pip install "$DIST_DIR"/*.whl
"$DIST_DIR/venv/bin/python" -c 'from importlib.metadata import version; import chaos_jungle; assert version("chaos-jungle") == chaos_jungle.__version__'
"$DIST_DIR/venv/bin/chaos-jungle" --help
```

Check that the built filenames and package version agree. GitLab's `build_dist`
and `verify_dist` jobs also check metadata, package contents, and installation.
Neither GitLab job publishes to PyPI.

## First alpha tag

The initial version is `0.1.0a0`. Do not run `cz bump` to create this first
version: no matching release tag exists yet. Before tagging, merge the reviewed
release changes to `main`, wait for GitHub and GitLab tests, and complete the
one-time publisher setup:

1. In PyPI, add a pending GitHub Trusted Publisher for owner `pegasus-isi`,
   repository `Chaos-jungle`, workflow `release.yml`, environment `pypi`.
2. In GitHub, create the `pypi` environment, require approval, and restrict
   who can push release tags.
3. Confirm your local `main` is clean and equals `pegasus/main` before tagging:

```sh
git fetch pegasus main
git status --short
git rev-parse HEAD
git rev-parse pegasus/main
git tag -a v0.1.0a0 -m "Chaos Jungle 0.1.0a0 alpha"
git push pegasus v0.1.0a0
```

The two printed commit IDs must match. Do not tag a dirty or unreviewed tree.
Pushing the tag triggers `.github/workflows/release.yml`: it reruns tests,
checks that the tag matches the version and points to a commit on `main`, builds
fresh distributions, and waits for `pypi` environment approval before upload.
No branch push publishes to PyPI. After approval, verify the PyPI page and a
fresh `python -m pip install --pre chaos-jungle` installation.

## Later alpha and beta versions

Start from clean, up-to-date `main` after the first release tag exists. Use
`cz bump --prerelease alpha` for another alpha, or `cz bump --prerelease beta`
when moving to beta. Commitizen updates `pyproject.toml`, `uv.lock`, the Python
and Sphinx version strings, the README version notice, and `CHANGELOG.md`; it
also creates the bump commit and `v`-prefixed tag. Review the commit and tag,
wait for branch CI, then push `main` followed by the specific new tag. Pushing
the tag starts the same GitHub release workflow. Do not automatically push or
publish from an assistant session without the user's explicit request.

## Automatic LLM experiments on GitLab

GitLab injects these project or group CI/CD variables into the job environment;
the YAML does not store or define their values:

| Variable | Meaning |
| --- | --- |
| `CJ_EVAL_BASE_URL` | HTTPS OpenAI-compatible endpoint ending in `/v1` |
| `CJ_EVAL_MODEL` | Model identifier accepted by that endpoint |
| `CJ_EVAL_API_KEY` | Bearer token for the endpoint |

Configure them in the GitLab project under **Settings > CI/CD > Variables**;
the default environment scope, **All**, is sufficient. Set the key as masked
and protected. Keep the endpoint and model protected if they are internal.
Since the current variables are protected, the three `llm_live_*` jobs run
automatically only on protected `main`. Each fails before installation if a
variable is missing; `ci/run_real_experiment.py` then validates the URL and
uses the values for its live calls. The three `llm_local_*` jobs use a local
mock service and need no LLM variables. Each job tests one of latency,
503-unavailable, or 429-rate-limit after `build_dist`, installing its wheel
rather than the editable checkout. The `agent_tool_result`,
`agent_tool_result_requests`, and `agent_prompt_input` jobs independently
check CJ's in-process injection against a local synthetic agent service; they
also run automatically after the wheel build. The `resource_cpu_experiment`
and `resource_memory_experiment` jobs run automatically on protected `main` after
the wheel build. They are serialized and run bounded stress
cases in separate CI job containers. The CPU case pins itself and its
children to one CPU at low priority; the memory case allocates 64 MiB after a
headroom check. CJ's opt-in preflight installs `stress-ng` in each container
before the baseline; the wheel installation does not install the system tool. Neither
case requires a runner-configured cgroup limit, but neither gives a hard host
resource limit. Keep them restricted to protected `main` and use a dedicated
runner when available; serialization prevents only this project's resource
jobs from overlapping, not other projects' workloads.
These jobs are smoke tests, not benchmarks. The live jobs make 24 endpoint
calls across the three cases per protected-branch pipeline and can fail if the
external service is unavailable. Their database and metrics artifacts may
contain prompts and model responses, so restrict artifact access and retention
accordingly.

## Skill-guided test strategy on GitLab

`skill_strategy_swarmplus` runs after `build_dist` on protected `main`. It clones
the public `swarm-workflows/SwarmAgents` default branch and records its exact
revision. `ci/generate_skill_strategy.py` inventories tracked source files,
excludes bulk results and secret-shaped paths, and sends a bounded summary
along with the two repository skills and relevant references to the configured
OpenAI-compatible model. It uses the same protected `CJ_EVAL_BASE_URL`,
`CJ_EVAL_MODEL`, and `CJ_EVAL_API_KEY` variables as the live LLM smoke tests.
The model sees selected public repository content; it does not get shell access.

Download the `ci-artifacts/skill-strategy/` artifact to review
`repository-inventory.json`, `strategy.json`, and `strategy.md`. The generator
checks the output shape and source-path citations; a human must still assess
whether each proposed fault, metric, and safety boundary is scientifically
appropriate. The job does not execute the strategy or inject faults. It is
initially allowed to fail without blocking the package pipeline because the
external model can be unavailable or return malformed JSON. The artifact
captures the SWARM+ revision and skill hashes, so a strategy can be traced to
the exact inputs. Local, uncommitted SWARM+ changes are not visible in GitLab.

See `docs/publishing.md` for the longer release checklist.
