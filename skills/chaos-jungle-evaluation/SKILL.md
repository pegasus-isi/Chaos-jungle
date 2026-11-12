---
name: chaos-jungle-evaluation
description: Build or review reproducible resilience evaluations of an application using Chaos Jungle. Use when the task involves workloads, controls, fault campaigns, exposure validation, application scoring, uncertainty, or evaluation artifacts; use the injection skill for a one-off fault placement task.
---

# Chaos Jungle Evaluation

Build an evaluation of the system under test (SUT), not a demonstration that CJ
can start a fault. CJ supplies fault control and boundary evidence; the
experimenter defines the workload, success criteria, and interpretation.

## Learn The System

Inspect the SUT, its tests, deployment boundary, existing telemetry, and the
installed CJ version before proposing a harness. Identify the operation that
must reach each fault, the expected normal behavior, and the application
output that can be scored independently of CJ. Use the actual package API and
documentation; do not invent fault parameters, metrics, or result fields.

If the target, authorization, or safe blast radius is unknown, resolve that
before running an infrastructure fault. Read the sibling
[injection skill](../chaos-jungle-injection/SKILL.md) when fault placement,
target selection, or proxy routing needs detailed work.

## Design The Comparison

For each use case, write down the workload, fault and rationale, hypothesis,
injection point, observation window, application score, operational metrics,
exposure evidence, recovery check, and safety bound. Read
[evaluation design](references/evaluation-design.md) when selecting controls,
replicates, or a campaign matrix.

Use comparable baseline and fault executions. Add a direct-without-CJ and a
CJ-pass-through control when instrumentation overhead or routing effects
matter; `runner.measure()` supplies baseline/fault trials but does not by
itself create that three-condition design. Keep task inputs and configuration
matched, record seeds where applicable, and account for time or provider
drift. A seed improves comparability but does not guarantee identical LLM
responses.

Choose application metrics for the SUT, such as task correctness, completion,
latency, cost, or recovery. CJ's recorded metrics and traces are inputs to
analysis, not automatically the right outcome definition. An optional judge
or oracle needs an explicit rubric and limitations; it is not proof that a
fault manifested.

## Build And Pilot

Unless the user asked only for a design, implement a runnable harness with
configuration, workload adapter, scoring, output paths, and focused tests.
Start with one bounded, disposable pilot. Verify routing and a healthy
control, then activation, trigger, intended effect, cleanup, and recovery.
Use `runner.exposure_report()` or mechanism-specific evidence supported by
the installed CJ version. Do not scale a campaign whose injection point has
not been observed.

Keep target-side dependencies on the target. Check declared command packages
with `chaos-jungle deps check --fault NAME` where supported; install only with
authorization. Never install on the host to prepare a container workload.
Use safety limits and timeouts, cleanup in `finally` when managing the fault
lifecycle manually, and explicit stop conditions for paid or live endpoints.

## Analyze Without Conflating Outcomes

Separate fault activation, valid exposure, restoration, and application
outcome. An active fault with no reached operation is not an observed
resilience success; missing effect evidence is not a valid exposure. A
manifested fault can remain a valid exposure even when recovery fails, so
report recovery separately rather than silently excluding it. For a grouped
injection, group activation alone is not proof that every member manifested.

Read [analysis and reporting](references/analysis-reporting.md) when computing
effects, denominators, uncertainty, or plots. Report both attempted-run and
valid-exposure counts, predeclared exclusions, and the exact population used
for each estimate. Do not fabricate measurements or infer confidence
intervals from minimum and maximum values.

## Deliver The Evaluation

Provide the runnable code or protocol, chosen metrics and rationale, safety
boundary, tests, and a concise result table or report. Preserve run config,
CJ version, task/seed identifiers, raw scores, exposure verdicts, and
recovery evidence in reproducible artifacts where permitted. Redact secrets
and sensitive prompts, tool arguments, and outputs before sharing artifacts.
State clearly what was executed, simulated, or left unverified.
