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

The automatic `llm_local_latency`, `llm_local_unavailable`, and
`llm_local_rate_limit` jobs run bounded integration experiments in disposable
CI containers after `build_dist`. They send HTTP calls through CJ's model
proxy to a local, deterministic OpenAI-compatible service, comparing baseline
calls with 120 ms latency, HTTP 503 unavailability, or HTTP 429 rate limiting.
They need no API credentials. Each job saves its own SQLite session database,
`metrics.json` (including default metrics and an exposure report), and
`summary.txt` under `ci-artifacts/real-experiment/<fault>/`. A failed exposure
or cleanup check fails that job.

The automatic `agent_tool_result`, `agent_tool_result_requests`, and
`agent_prompt_input` jobs exercise CJ's in-process `inject(...)` path against
another local HTTP service. The first two use HTTPX and Requests respectively
to replace a search tool's result without changing HTTP 200; the third changes
an outgoing user prompt. Each case checks the request seen by the service, the
resulting answer, the trace, and the restored behavior after the block exits.
Its database, metrics, and summary are under `ci-artifacts/agent-use-case/`.
These are synthetic agent-workflow checks, not tests of a particular model's
judgment or of a third-party agent framework.

For an actual model endpoint, set `CJ_EVAL_BASE_URL` to an OpenAI-compatible
base URL ending in `/v1`, `CJ_EVAL_MODEL` to a model name, and
`CJ_EVAL_API_KEY` to a masked, protected GitLab CI/CD variable. The separate
`llm_live_latency`, `llm_live_unavailable`, and `llm_live_rate_limit` jobs run
automatically only on the protected default branch. They fail early if a
variable is missing. GitLab injects the values; the YAML never stores them.
The live jobs use a 250 ms delay in the latency case and a small token limit
on requests. Each job has two baseline and two fault trials, with two model
calls per trial (eight endpoint calls per job, 24 across all three). They
produce separate databases, JSON metrics, and summaries. A live endpoint
outage, authorization failure, or changed model behavior can now fail the
protected-branch pipeline; these jobs also consume endpoint quota. The endpoint
must be reachable from the Docker runner; loopback URLs refer to the job
container, not your laptop. The database captures the synthetic prompt and
model response, so restrict artifact access if those responses are sensitive.
Do not place credentials in the base URL.
These jobs install the wheel produced by `build_dist` and run from `/tmp`, so
the imports resolve to the built package rather than the source checkout. They
are integration smoke checks, not benchmark estimates; two trials per
condition are too few for stable latency percentiles or confidence intervals.

The `resource_cpu_experiment` and `resource_memory_experiment` jobs run
automatically on protected `main` after `build_dist` and install its wheel.
They are serialized and run independent CJ
`CPUStress` and `MemoryStress` scenarios in separate CI job containers,
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
is required, but affinity and low priority are not hard host isolation.
Serialization prevents these two jobs from overlapping, not jobs from other
projects; a dedicated runner is preferable. These short comparisons are smoke
checks, not performance estimates.

For live-run diagnosis, each job logs the endpoint scheme, host, port, and
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

The two AI-assistant skills are released independently of the Python package.
`ci/build_skill_archives.py` creates one versioned ZIP per skill, preserving
its `SKILL.md` and supporting files under one top-level skill directory.
These ZIPs are GitHub Release assets, not PyPI distribution files. Build them
locally with `python ci/build_skill_archives.py --version 0.1.0a0 --output-dir dist`
and inspect the archive entries before tagging.

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
That tag now exists; do not move it when repairing a release workflow.

For a GitHub release, inspect a fresh local build and confirm that the tag
points to the intended commit on `main`. Pushing a new version tag runs
`.github/workflows/release.yml`. It tests Python 3.9-3.12 on Ubuntu 24.04,
checks the tag and package versions, builds and checks the wheel, source
distribution, and two standalone skill ZIPs, then creates a GitHub prerelease
with all four assets. PyPI publishing is not configured.

If the workflow attached to an existing tag failed, do not force-push the tag.
After fixing the workflow on `main`, use **Actions > Release Chaos Jungle > Run
workflow** on `main` and supply the existing tag in the `tag` input. This
checks out the tag's source while using the corrected workflow definition.
Only run it for a tag whose matching GitHub Release does not yet exist.
Existing local `dist/` files may be stale; never upload them instead of a
clean tag build.
