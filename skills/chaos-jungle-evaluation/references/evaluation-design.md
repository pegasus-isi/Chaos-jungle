# Evaluation Design

Use this reference when translating a SUT into a reproducible CJ experiment.

## One Row Per Use Case

Record these decisions before implementation:

| Decision | Question to answer |
| --- | --- |
| Workload | What real task or request runs, and what normal output is expected? |
| Fault | Which failure is plausible, why does it matter, and where can CJ place it? |
| Reachability | What operation must occur for the fault to be selected and triggered? |
| Hypothesis | What observable behavior should the SUT preserve or recover? |
| Application score | What independent oracle measures task success or quality? |
| Operational measures | Which latency, retry, cost, resource, or status measures explain the response? |
| Exposure evidence | What proves activation, trigger, and the intended effect at this boundary? |
| Recovery and safety | What proves restoration, and what aborts the experiment? |

Prefer a small matrix of distinct failure mechanisms over many parameter
variants with the same explanation. Make the fault intensity and duration
credible for the target, and bound paid calls, resource use, and concurrent
workloads. An injection that does not reach the workload answers a coverage
question, not a resilience question.

## Conditions And Repeats

- A baseline/fault pair is sufficient when both conditions use the same CJ
  path and the question concerns the incremental effect of the fault.
- Add direct (no CJ), CJ pass-through (no fault), and CJ fault conditions when
  proxy overhead, routing, or instrumentation could affect results. Compare
  pass-through with direct before attributing a difference to the fault.
- Match tasks and supported pseudorandom seeds across conditions. Record model,
  prompt, tools, versions, target, fault parameters, and configuration. A seed
  does not make a nondeterministic model deterministic.
- Plan repetitions from the variability and effect size that matter; a pilot
  can inform the count. Counterbalance or interleave conditions when system
  load, model service, or time can drift.
- Define the primary estimand before inspecting outcomes: all attempted
  trials, trials with verified exposure, or another explicitly named subset.
  Keep failures, missing data, and recovery status visible in the run ledger.

## Implementation Boundary

Use the narrowest CJ interface that reaches the operation. The installed
package documentation is the source for current constructors and results.
`ChaosRunner.measure()` is convenient for baseline/fault trials and automatic
metrics; a custom harness may be needed for a direct/CJ-control/fault
comparison, matched task IDs, external scoring, or clustered analysis.

For containerized SUTs, prepare tools and capabilities inside a disposable
container; do not provision the host by accident. For LLM or tool faults,
verify that the actual client uses the intended proxy or interception path.
For target-side faults, arrange a mechanism-specific observation during the
fault window. Store the run ledger and output artifacts outside disposable
containers, with access controls and redaction where necessary.
