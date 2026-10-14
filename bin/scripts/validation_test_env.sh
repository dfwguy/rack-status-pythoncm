#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# VALIDATION_TEST: comma-separated suite for rack "done" (nccl_ok ∧ llm_ok in tmp_status).
# Default: nccl_all_reduce,nccl_alltoall,nccl_allgather,llm
# Tokens (case-insensitive): nccl_all_reduce|all_reduce|allreduce,
#   nccl_alltoall|alltoall|nccl_all_toall, nccl_allgather|all_gather|allgather, llm|nemotron.
# Omit NCCL tokens → no NCCL busbw required; omit llm → LLM not required for pass.
# Source this file after llm_label is defined. nemo_training_finished_log is defined here only if missing.

# Fallback only — lib/llm_label.inc.sh owns the real definition. Keep the semantics identical:
# finished means the run reached the target it declared, with NEMO_TARGET_STEPS as an opt-in
# acceptance floor (unset = accept the declared target, so 50-step runs count).
if ! declare -F nemo_training_finished_log &>/dev/null; then
  nemo_training_finished_log() {
    local f=$1 ms="${NEMO_TARGET_STEPS:-0}" last cur tot
    [[ "$ms" =~ ^[0-9]+$ ]] || ms=0
    last=$(grep -oE 'iteration[[:space:]]+[0-9]+/[[:space:]]*[0-9]+' "$f" 2>/dev/null | tail -1) || true
    [[ -n "$last" ]] || return 1
    [[ "$last" =~ iteration[[:space:]]+([0-9]+)/[[:space:]]*([0-9]+) ]] || return 1
    cur=$((10#${BASH_REMATCH[1]}))
    tot=$((10#${BASH_REMATCH[2]}))
    ((tot > 0 && cur >= tot && tot >= ms))
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

# Args: f ongoing ar_bw aa_bw ag_bw [llm_line] [finished]
# Optional 6th arg: precomputed "$(llm_label "$f")" to avoid calling llm_label twice.
# Optional 7th arg: precomputed completion (0/1). Pass it when a single-pass scanner already
# answered "did it reach its declared target", so we do not re-read the whole log to ask again.
# Sets _OUT_NCCL_OK and _OUT_LLM_OK (0 or 1). Uses llm_label from the sourcing script if arg 6 omitted.
validation_eval_suite_for_file() {
  local f=$1 ongoing=$2 ar_bw=$3 aa_bw=$4 ag_bw=$5 llm_line finished="${7:-}"
  _validation_test_ensure_parsed
  _OUT_NCCL_OK=1
  _OUT_NCCL_ISSUE=""
  ((WANT_AR)) && { [[ -n "${ar_bw:-}" ]] || _OUT_NCCL_OK=0; }
  ((WANT_AA)) && { [[ -n "${aa_bw:-}" ]] || _OUT_NCCL_OK=0; }
  ((WANT_AG)) && { [[ -n "${ag_bw:-}" ]] || _OUT_NCCL_OK=0; }
  # A wanted peak is not a pass on its own: wrong results, a failed or an unfinished test anywhere
  # in the log fail it too (nccl_integrity_issue in nccl_parse.inc.sh). The reason is kept for callers.
  if ((WANT_AR || WANT_AA || WANT_AG)) && declare -F nccl_integrity_issue >/dev/null 2>&1; then
    _OUT_NCCL_ISSUE=$(nccl_integrity_issue "$f")
    [[ -n "$_OUT_NCCL_ISSUE" ]] && _OUT_NCCL_OK=0
  fi
  if [[ $# -ge 6 ]]; then
    llm_line=$6
  else
    llm_line=$(llm_label "$f")
  fi
  _OUT_LLM_OK=1
  if ((WANT_LLM)); then
    _OUT_LLM_OK=0
    if [[ "${llm_line%%$'\t'*}" == "OK" ]]; then
      if [[ "$ongoing" -eq 1 ]]; then
        _OUT_LLM_OK=1
      elif [[ -n "$finished" ]]; then
        [[ "$finished" == "1" ]] && _OUT_LLM_OK=1
      elif nemo_training_finished_log "$f"; then
        _OUT_LLM_OK=1
      fi
    fi
  fi
}
