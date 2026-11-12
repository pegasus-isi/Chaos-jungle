# Building and publishing Chaos Jungle

The first public release is the alpha pre-release `0.1.0a0`. Python 3.9
remains supported for package users and tests. Release tooling (Commitizen and
pre-commit) requires Python 3.10 or newer. Version and release tags follow
PEP 440: for example, `0.1.0a0` in package metadata and `v0.1.0a0` in Git.

The GitLab pipeline runs the core test suite on Python 3.9-3.12, checks Ruff,
builds a wheel and source distribution, validates their metadata, and installs
the wheel for a smoke test. Download the `build_dist` job artifacts to inspect
the exact files that would be published. Runtime scripts under
`chaos_jungle/scripts/`, including `bpf/flow_modify.c`, must remain in the wheel.
The `docs_build` job also renders this Sphinx manual and makes its HTML output
available as a GitLab job artifact for review.

The optional `real_experiment` job runs a bounded integration experiment in its
disposable CI container. It makes real HTTP calls through CJ's model proxy to
a local, deterministic OpenAI-compatible service, first without a fault and
then with a latency fault. It does not call an external LLM or require API
credentials. Start it manually from a branch pipeline. Its
`ci-artifacts/real-experiment/` artifact contains the SQLite session database,
`metrics.json` (including the fault's declared default metrics and exposure
report), and `summary.txt`. The job checks fault manifestation and cleanup; it
is optional, so a skipped or failed experiment does not block packaging.

For an actual model endpoint, set `CJ_EVAL_BASE_URL` to an OpenAI-compatible
base URL ending in `/v1`, `CJ_EVAL_MODEL` to a model name, and
`CJ_EVAL_API_KEY` to a masked GitLab CI/CD variable. Then start the separate
manual `real_llm_experiment` job from a protected branch such as `main` when
using protected variables. GitLab injects these variables into the job; the
YAML checks that they are present but never contains their values. It sends
four baseline and four fault requests
with a small token limit and adds a 250 ms model-proxy delay. Its artifact has
the same database, JSON metrics, and summary as the local job. The endpoint
must be reachable from the Docker runner; loopback URLs refer to the job
container, not your laptop. The database captures the synthetic prompt and
model response, so restrict artifact access if those responses are sensitive.
Do not place credentials in the base URL.
Both jobs are integration smoke checks, not benchmark estimates; their two
trials per condition are too few for stable latency percentiles or confidence
intervals.

The source distribution is limited to the package, README, license, and build
metadata. It does not need experiment archives, evaluation data, local virtual
environments, secrets, or generated results. The `.gitignore` file prevents
local artifacts from being committed; the Hatchling `sdist` allowlist controls
what enters a source release. Check both before each release.

To test a release locally, use a virtual environment with Python 3.10 or newer:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pip install --upgrade build twine
pre-commit install --hook-type pre-commit --hook-type commit-msg
pre-commit run --all-files
cz version --project
DIST_DIR=$(mktemp -d)
python -m build --outdir "$DIST_DIR"
python -m twine check "$DIST_DIR"/*
python -m pip install "$DIST_DIR"/*.whl
chaos-jungle --help
```

Commitizen uses Conventional Commits, creates `v`-prefixed release tags, and
updates `pyproject.toml`, `uv.lock`, `chaos_jungle/__init__.py`, `docs/conf.py`,
the README version notice, and `CHANGELOG.md` on later bumps. Run
`cz bump --prerelease alpha` (or
`--prerelease beta`) when you are ready for a *new* version. Review the bump
commit and tag before pushing. The initial `v0.1.0a0` release tag should be
created manually after this release-preparation change is merged to `main`.

For PyPI publication from GitHub:

1. Check that the `chaos-jungle` name is available on PyPI. On PyPI, add a
   **pending GitHub Trusted Publisher** for owner `pegasus-isi`, repository
   `Chaos-jungle`, workflow `release.yml`, and environment `pypi`. The owner
   must complete this step; the workflow cannot create the publisher.
2. In GitHub repository settings, create a `pypi` environment and require
   reviewer approval for the publishing job. Restrict who can create release
   tags, and confirm the code and version on `main` before tagging.
3. Confirm GitLab and GitHub tests pass, and inspect a fresh local build. For
   the first release, create and push `v0.1.0a0` pointing at the reviewed
   `main` commit. For later releases, push the tag created by `cz bump`.
4. The GitHub tag workflow reruns tests, verifies that the tag matches package
   metadata and points to a commit on `main`, builds and checks both
   distributions, smoke-tests the wheel, then waits for `pypi` environment
   approval before publishing. Confirm the resulting PyPI page and install it
   in a fresh environment with `python -m pip install --pre chaos-jungle`.

This project's self-managed GitLab instance is not eligible for PyPI's GitLab
Trusted Publishing integration. GitLab continues to test and build, but only
the GitHub tag workflow publishes. The GitHub workflow uses short-lived OIDC
credentials, so do not commit a PyPI token or upload a local `.env`. Existing
`dist/` files may be stale; never upload them instead of a clean tag build.
