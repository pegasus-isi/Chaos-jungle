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

## Optional live LLM experiment on GitLab

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
Since the current variables are protected, run the manual
`real_llm_experiment` job from the protected `main` branch. The job fails before
installation if a variable is missing; `ci/run_real_experiment.py` then checks
the URL and uses the values for its live calls. The separate `real_experiment`
job uses a local mock service and needs no LLM variables. Neither job is a
benchmark. The live job's database and metrics artifact may contain prompts
and model responses, so restrict artifact access and retention accordingly.

See `docs/publishing.md` for the longer release checklist.
