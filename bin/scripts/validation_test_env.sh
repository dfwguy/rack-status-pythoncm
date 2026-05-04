#!/usr/bin/env bash
# VALIDATION_TEST: comma-separated suite for rack "done" (nccl_ok ∧ llm_ok in tmp_status).
# Default: nccl_all_reduce,nccl_alltoall,nccl_allgather,llm
# Tokens (case-insensitive): nccl_all_reduce|all_reduce|allreduce,
#   nccl_alltoall|alltoall|nccl_all_toall, nccl_allgather|all_gather|allgather, llm|nemotron.
# Omit NCCL tokens → no NCCL busbw required; omit llm → LLM not required for pass.
# Source this file after llm_label is defined. nemo_training_finished_log is defined here only if missing.

if ! declare -F nemo_training_finished_log &>/dev/null; then
  nemo_training_finished_log() {
    local f=$1 ms="${NEMO_TARGET_STEPS:-2010}" last cur tot
    [[ "$ms" =~ ^[0-9]+$ ]] || ms=2010
    last=$(grep -oE 'iteration[[:space:]]+[0-9]+/[[:space:]]*[0-9]+' "$f" 2>/dev/null | tail -1) || true
    [[ -n "$last" ]] || return 1
    [[ "$last" =~ iteration[[:space:]]+([0-9]+)/[[:space:]]*([0-9]+) ]] || return 1
    cur=$((10#${BASH_REMATCH[1]}))
    tot=$((10#${BASH_REMATCH[2]}))
    ((cur >= tot && tot >= ms))
  }
fi

validation_test_raw() {
  local r="${VALIDATION_TEST-}"
  r="${r//[[:space:]]/}"
  if [[ -z "$r" ]]; then
    r="nccl_all_reduce,nccl_alltoall,nccl_allgather,llm"
  fi
  printf '%s' "$r"
}

_VALIDATION_TEST_PARSED=0
_validation_test_ensure_parsed() {
  if ((_VALIDATION_TEST_PARSED)); then
    return 0
  fi
  WANT_AR=0 WANT_AA=0 WANT_AG=0 WANT_LLM=0
  local tok raw
  raw="$(validation_test_raw)"
  IFS=',' read -ra _vtoks <<<"$raw"
  for tok in "${_vtoks[@]}"; do
    [[ -n "$tok" ]] || continue
    case "${tok,,}" in
      nccl_all_reduce | all_reduce | allreduce) WANT_AR=1 ;;
      nccl_alltoall | alltoall | nccl_all_toall) WANT_AA=1 ;;
      nccl_allgather | all_gather | allgather) WANT_AG=1 ;;
      llm | nemotron) WANT_LLM=1 ;;
    esac
  done
  _VALIDATION_TEST_PARSED=1
}

# Args: f ongoing ar_bw aa_bw ag_bw [llm_line]
# Optional 6th arg: precomputed "$(llm_label "$f")" to avoid calling llm_label twice.
# Sets _OUT_NCCL_OK and _OUT_LLM_OK (0 or 1). Uses llm_label from the sourcing script if arg 6 omitted.
validation_eval_suite_for_file() {
  local f=$1 ongoing=$2 ar_bw=$3 aa_bw=$4 ag_bw=$5 llm_line
  _validation_test_ensure_parsed
  _OUT_NCCL_OK=1
  ((WANT_AR)) && { [[ -n "${ar_bw:-}" ]] || _OUT_NCCL_OK=0; }
  ((WANT_AA)) && { [[ -n "${aa_bw:-}" ]] || _OUT_NCCL_OK=0; }
  ((WANT_AG)) && { [[ -n "${ag_bw:-}" ]] || _OUT_NCCL_OK=0; }
  if [[ $# -ge 6 ]]; then
    llm_line=$6
  else
    llm_line=$(llm_label "$f")
  fi
  _OUT_LLM_OK=1
  if ((WANT_LLM)); then
    _OUT_LLM_OK=0
    if [[ "${llm_line%%$'\t'*}" == "OK" ]]; then
      if [[ "$ongoing" -eq 1 ]] || nemo_training_finished_log "$f"; then
        _OUT_LLM_OK=1
      fi
    fi
  fi
}
