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

The optional `real_experiment` job runs bounded integration experiments in its
disposable CI container. It sends real HTTP calls through CJ's model proxy to
a local, deterministic OpenAI-compatible service, comparing baseline calls
with three separate fault scenarios: 120 ms latency, HTTP 503 unavailability,
and HTTP 429 rate limiting. It does not call an external LLM or require API
credentials. Start it manually from a branch pipeline. Each scenario has its
own SQLite session database, `metrics.json` (including the fault's declared
default metrics and exposure report), and `summary.txt` under
`ci-artifacts/real-experiment/`. The job checks the expected response,
exposure, and cleanup; it is optional, so a skipped or failed experiment does
not block packaging.

For an actual model endpoint, set `CJ_EVAL_BASE_URL` to an OpenAI-compatible
base URL ending in `/v1`, `CJ_EVAL_MODEL` to a model name, and
`CJ_EVAL_API_KEY` to a masked GitLab CI/CD variable. Then start the separate
manual `real_llm_experiment` job from a protected branch such as `main` when
using protected variables. GitLab injects these variables into the job; the
YAML checks that they are present but never contains their values. The live
job repeats the same three scenarios, with a 250 ms delay in the latency case
and a small token limit on requests. Each case has two baseline and two fault
trials, with two model calls per trial. Its artifact has a separate database,
JSON metrics, and summary for each case. The endpoint
must be reachable from the Docker runner; loopback URLs refer to the job
container, not your laptop. The database captures the synthetic prompt and
model response, so restrict artifact access if those responses are sensitive.
Do not place credentials in the base URL.
These jobs install the wheel produced by `build_dist` and run from `/tmp`, so
the imports resolve to the built package rather than the source checkout. They
are integration smoke checks, not benchmark estimates; two trials per
condition are too few for stable latency percentiles or confidence intervals.

The optional `resource_cpu_experiment` and `resource_memory_experiment` jobs
each wait for `build_dist` and install its wheel. They run independent CJ
`CPUStress` and `MemoryStress` scenarios in separate Docker job containers,
each with two baseline and two fault trials.
After refreshing the Debian package index, the script uses CJ preflight's
opt-in `auto_install=True` to install `stress-ng` inside that disposable
container before measuring the baseline. Installing the wheel alone does not
install the system tool. Each job has its own database and metrics artifacts.
The CPU case pins the experiment and its children to one allowed CPU and lowers
their priority; the memory case uses 64 MiB after checking available headroom.
Both faults auto-expire after five seconds, and CJ stops them after measurement.
The job saves separate databases, metrics, and summaries under
`ci-artifacts/resource-experiment/cpu/` and `memory/`. CPU evidence is the
stress worker's CPU time; memory evidence is its resident memory. The result
also reports workload throughput, activation, and cleanup. No runner CPU quota
is required, but affinity and low priority are not hard host isolation: keep
this job manual on shared runners. These short comparisons are smoke checks,
not performance estimates.

For live-run diagnosis, the job logs the endpoint scheme, host, port, and
path, the configured model, and whether an API key is present. It never logs
the key. GitLab may redact the model value because it is configured as a masked
variable. These checks run before the baseline request, so an HTTP 403 from
that request points to endpoint access, authorization, or model permissions,
not fault injection.

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
