# Analysis And Reporting

Use this reference after a pilot or campaign produces records. Preserve the
raw data and analysis code so each displayed value can be reproduced.

## Classify Before Scoring

For each attempt, retain task/seed, condition, fault, target, run status,
activation, trigger, manifestation, policy compliance, reversion, recovery,
application score, and evidence source. Use CJ's reported exposure verdict
where available: `valid`, `untriggered`, `invalid`, or `inconclusive`.
Do not convert missing observations into `untriggered`, or use task failure as
evidence that injection worked. Keep the lifecycle checks distinct.

State the denominator beside every number. A useful flow is attempts, verified
activations, triggered operations, manifested exposures, recovered runs, and
scored application outcomes. Report the attempted-run rate as well as any
exposure-conditioned estimate. If zero runs have verified exposure, do not
compute a fault-impact estimate and call it resilience.

## Estimate Effects

- For binary task success, compare rates in percentage points, with the
  condition and denominator specified. A percentage-point difference is not
  a relative percent change.
- For positive durations or costs, a paired fault/control ratio is often more
  interpretable than an absolute difference. Explain ratios above or below 1
  and do not describe a halving as a negative twofold increase.
- Show operational measures such as retries, latency, cost, and resource use
  alongside task quality when they explain fallback or silent failure.
- When multiple seeds belong to one task, treat the task as the resampling
  unit for uncertainty; do not pretend its seeds are independent tasks.
  State the interval method, number of resamples if used, and interval level.
  Whiskers for a confidence interval are not observed minimum and maximum.
- A small pilot may justify descriptive counts only. Do not make population
  claims from a few handpicked examples.

## Communicate The Result

Each figure or table should identify conditions, unit, direction of better
performance, sample size, and interval meaning. Include a concrete example
of observed failure or successful recovery when it clarifies the mechanism,
but distinguish a representative trace from aggregate evidence. Explain why
cases were excluded before comparing scores; do not use failed recovery alone
to erase a manifested exposure. For coordinated faults, label estimates as
activation-conditioned unless every member's exposure was established.

Keep CJ's boundary evidence and the experimenter's task score distinguishable
in the exported database, plots, and prose. Record limitations that affect
inference, especially incomplete targeting, provider variability, missing
observations, and untested environments. Redact secrets and sensitive trace
content before publication or CI artifact upload.
