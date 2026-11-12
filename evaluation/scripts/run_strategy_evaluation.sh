#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"
cd "${CJ_EVAL_REPO_ROOT}"

profile="${CJ_EVAL_STRATEGY_PROFILE:-paper}"
results_run="${CJ_EVAL_EXPERIMENT_RUN:-strategy-fidelity-$(cj_eval_timestamp)}"
requests=""
selector_requests=""
seeds=""
probabilities="${CJ_EVAL_STRATEGY_PROBABILITIES:-0,0.1,0.25,0.5,0.75,1}"
cycles=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) profile="$2"; shift 2 ;;
    --results-run|--experiment-run) results_run="$2"; shift 2 ;;
    --requests) requests="$2"; shift 2 ;;
    --selector-requests) selector_requests="$2"; shift 2 ;;
    --seeds) seeds="$2"; shift 2 ;;
    --probabilities) probabilities="$2"; shift 2 ;;
    --cycles) cycles="$2"; shift 2 ;;
    -h|--help)
      cat <<'EOF'
Evaluate CJ's documented injection strategies against a real loopback HTTP service.

Usage:
  evaluation/scripts/run_strategy_evaluation.sh [options]

Options:
  --profile smoke|paper    smoke is a quick gate; paper uses larger samples.
  --results-run NAME       top-level results folder name.
  --requests N             requests per probability and progressive step.
  --selector-requests N    target and non-target requests per selector.
  --seeds LIST             comma-separated random seeds.
  --probabilities LIST     comma-separated probability ramp in [0,1].
  --cycles N               fault/rest cycles for the door strategy.

This study is no-cost: it makes no model calls and does not require Docker.
It does not replace or modify the existing infrastructure campaign.
EOF
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

case "${profile}" in
  smoke)
    requests="${requests:-100}"
    selector_requests="${selector_requests:-20}"
    seeds="${seeds:-11}"
    cycles="${cycles:-3}"
    ;;
  paper)
    requests="${requests:-1000}"
    selector_requests="${selector_requests:-200}"
    seeds="${seeds:-11,29,43}"
    cycles="${cycles:-20}"
    ;;
  *)
    echo "Unknown strategy profile '${profile}'; expected smoke or paper" >&2
    exit 2
    ;;
esac

export CJ_EVAL_EXPERIMENT_RUN="${results_run}"
export CJ_EVAL_EXPERIMENT_CREATED_AT="${CJ_EVAL_EXPERIMENT_CREATED_AT:-$(cj_eval_timestamp)}"
cj_eval_init_experiment_run

category="strategy-fidelity"
name="E10-${profile}"
checkpoint="strategy-fidelity-profile=${profile}-requests=${requests}-selectors=${selector_requests}-seeds=${seeds}-probabilities=${probabilities}-cycles=${cycles}"
title="E10 strategy fidelity (${profile})"

if cj_eval_checkpoint_done "${checkpoint}"; then
  cj_eval_skip_checkpoint_message "${checkpoint}" "${title}"
  exit 0
fi

if cj_eval_backfill_checkpoint_from_run_dir \
  "${checkpoint}" "${title}" "${category}" "${name}" "strategy_fidelity_summary.json"; then
  exit 0
fi

output_dir="$(cj_eval_make_run_dir "${category}" "${name}")"
cmd=(
  "${CJ_EVAL_PYTHON}" -m evaluation.strategy_campaign
  --results-dir "${output_dir}"
  --requests-per-probability "${requests}"
  --selector-requests "${selector_requests}"
  --seeds "${seeds}"
  --probabilities "${probabilities}"
  --cycles "${cycles}"
  --phase-duration-s "${CJ_EVAL_STRATEGY_PHASE_DURATION_S:-0.01}"
  --after-n-calls "${CJ_EVAL_STRATEGY_AFTER_N_CALLS:-3}"
  --scheduler-interval-s "${CJ_EVAL_STRATEGY_SCHEDULER_INTERVAL_S:-0.05}"
  --scheduler-window-s "${CJ_EVAL_STRATEGY_SCHEDULER_WINDOW_S:-0.35}"
)

mkdir -p "${output_dir}/logs"
cj_eval_write_command "${output_dir}/run_command.sh" "${cmd[@]}"
cat <<EOF
[cj-eval] strategy fidelity
[cj-eval] profile  : ${profile}
[cj-eval] requests : ${requests} per probability/step
[cj-eval] seeds    : ${seeds}
[cj-eval] cycles   : ${cycles}
[cj-eval] results  : ${output_dir}
[cj-eval] cost     : no model calls; no Docker required
EOF
cj_eval_run_logged "${output_dir}/logs/run.log" "${cmd[@]}"
cj_eval_mark_checkpoint_done "${checkpoint}" "${title}"
echo "[cj-eval] strategy-fidelity study complete: ${output_dir}"
