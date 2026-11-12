#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  evaluation/scripts/run_controlled_infrastructure_resilience.sh [options]

Run a bounded real distributed-agent campaign covering:
  - coder-container network delay (150 ms by default);
  - coder-container packet loss (10% by default);
  - coder-container memory pressure (256 MiB by default);
  - event-triggered coder role-process termination.

Every experiment uses direct_baseline, cj_control, and cj_fault conditions.
The defaults run 3 frameworks x 2 benchmarks x 1 topology x 3 tasks x 4
faults = 72 triplets (216 executions) for one seed.

Options:
  --env-file PATH       Model environment file. Default: .env.
  --model NAME          Model identifier. Default: .env or minimax-m2.
  --systems LIST        Default: autogen-real langgraph-real crewai-real.
  --benchmarks LIST     Default: humanevalplus mbppplus.
  --topologies LIST     Default: linear.
  --tasks N             Tasks per benchmark. Default: 3.
  --seeds LIST          Execution seeds. Default: 17.
  --task-selection-seed N Fixed task manifest seed. Default: 17.
  --network-delay-ms N  Default: 150.
  --network-loss-pct N  Default: 10.
  --memory-pressure-mb N Default: 256.
  --process-trigger-timeout N Default: 45 seconds.
  --fault-duration S    Memory-pressure safety duration. Default: 120.
  --target-role ROLE    Default: coder.
  --results-run NAME    Folder name under results/.
  --help                Show this help.
EOF
}

systems="${CJ_EVAL_SYSTEMS:-autogen-real langgraph-real crewai-real}"
benchmarks="${CJ_EVAL_BENCHMARKS:-humanevalplus mbppplus}"
topologies="${CJ_EVAL_TOPOLOGIES:-linear}"
tasks="${CJ_EVAL_TASKS:-3}"
seeds="${CJ_EVAL_SEEDS:-${CJ_EVAL_SEED:-17}}"
task_selection_seed="${CJ_EVAL_TASK_SELECTION_SEED:-17}"
network_delay_ms="${CJ_EVAL_DISTRIBUTED_NETWORK_DELAY_MS:-150}"
network_loss_pct="${CJ_EVAL_DISTRIBUTED_NETWORK_LOSS_PCT:-10}"
memory_pressure_mb="${CJ_EVAL_DISTRIBUTED_MEMORY_PRESSURE_MB:-256}"
fault_duration_s="${CJ_EVAL_DISTRIBUTED_FAULT_DURATION_S:-120}"
process_trigger_timeout_s="${CJ_EVAL_DISTRIBUTED_PROCESS_TRIGGER_TIMEOUT_S:-45}"
target_role="${CJ_EVAL_DISTRIBUTED_FAULT_TARGET_ROLE:-coder}"

normalize_list() { echo "$1" | tr ',' ' '; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) export CJ_EVAL_ENV_FILE="$2"; shift 2 ;;
    --model) export CJ_EVAL_MODEL="$2"; shift 2 ;;
    --systems) systems="$(normalize_list "$2")"; shift 2 ;;
    --benchmarks) benchmarks="$(normalize_list "$2")"; shift 2 ;;
    --topologies) topologies="$(normalize_list "$2")"; shift 2 ;;
    --tasks) tasks="$2"; shift 2 ;;
    --seeds) seeds="$(normalize_list "$2")"; shift 2 ;;
    --task-selection-seed) task_selection_seed="$2"; shift 2 ;;
    --network-delay-ms) network_delay_ms="$2"; shift 2 ;;
    --network-loss-pct) network_loss_pct="$2"; shift 2 ;;
    --memory-pressure-mb) memory_pressure_mb="$2"; shift 2 ;;
    --process-trigger-timeout) process_trigger_timeout_s="$2"; shift 2 ;;
    --fault-duration) fault_duration_s="$2"; shift 2 ;;
    --target-role) target_role="$2"; shift 2 ;;
    --results-run) export CJ_EVAL_EXPERIMENT_RUN="$2"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "[cj-eval] unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

cd "${CJ_EVAL_REPO_ROOT}"
cj_eval_model_defaults
export CJ_EVAL_RUN_STAMP="${CJ_EVAL_RUN_STAMP:-$(cj_eval_timestamp)}"
export CJ_EVAL_EXPERIMENT_RUN="${CJ_EVAL_EXPERIMENT_RUN:-controlled-infrastructure-${CJ_EVAL_RUN_STAMP}}"
cj_eval_init_experiment_run

image="$(cj_eval_default_image)"
cj_eval_require_image "${image}"
export CJ_EVAL_DOCKER_IMAGE="${image}"

cat <<EOF
[cj-eval] controlled infrastructure-resilience campaign
[cj-eval] results     : $(cj_eval_results_root)
[cj-eval] image       : ${image}
[cj-eval] model       : ${CJ_EVAL_MODEL}
[cj-eval] systems     : ${systems}
[cj-eval] benchmarks  : ${benchmarks}
[cj-eval] topologies  : ${topologies}
[cj-eval] tasks       : ${tasks}
[cj-eval] seeds       : ${seeds}
[cj-eval] target role : ${target_role}
[cj-eval] faults      : network delay, packet loss, memory pressure, process kill
EOF

for seed in ${seeds}; do
  echo "[cj-eval] starting controlled infrastructure seed ${seed}"
  CJ_EVAL_SEED="${seed}" \
  CJ_EVAL_DISTRIBUTED_NATIVE_SYSTEMS="${systems}" \
  CJ_EVAL_DISTRIBUTED_NATIVE_BENCHMARKS="${benchmarks}" \
  CJ_EVAL_DISTRIBUTED_NATIVE_TOPOLOGIES="${topologies}" \
  CJ_EVAL_DISTRIBUTED_NATIVE_TASKS="${tasks}" \
  CJ_EVAL_TASK_SELECTION_SEED="${task_selection_seed}" \
  CJ_EVAL_DISTRIBUTED_NATIVE_SUBSET="${CJ_EVAL_BENCHMARK_SUBSET:-development}" \
  CJ_EVAL_DISTRIBUTED_NATIVE_FAULT_SUITE="infrastructure_controlled" \
  CJ_EVAL_DISTRIBUTED_NATIVE_TRIPLET=1 \
  CJ_EVAL_DISTRIBUTED_FAULT_TARGET_ROLE="${target_role}" \
  CJ_EVAL_DISTRIBUTED_FAULT_DURATION_S="${fault_duration_s}" \
  CJ_EVAL_DISTRIBUTED_NETWORK_DELAY_MS="${network_delay_ms}" \
  CJ_EVAL_DISTRIBUTED_NETWORK_LOSS_PCT="${network_loss_pct}" \
  CJ_EVAL_DISTRIBUTED_MEMORY_PRESSURE_MB="${memory_pressure_mb}" \
  CJ_EVAL_DISTRIBUTED_PROCESS_TRIGGER_TIMEOUT_S="${process_trigger_timeout_s}" \
    "${script_dir}/run_distributed_native_multi_agent_smoke.sh"
done

echo "[cj-eval] controlled infrastructure campaign complete: $(cj_eval_results_root)"
