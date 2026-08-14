# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# shellcheck shell=bash
# lib/llm_label.inc.sh — the ONE definition of the NeMo/LLM verdict for a validation log.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Source (do not execute). Shared by rack_status.sh (which answers the dashboard via
# --probe-json) and parser.sh (which builds the Parser Results tables), so a rack cannot
# pass on one surface and read UNK on the other.
#
# Why this file exists: both scripts used to carry their own copy of llm_label, and the
# copies drifted. parser.sh's copy checked only `llm_stats_tsv` then `slurm_hard_fail`,
# so a run that completed its target iterations WITHOUT step-timing lines in the 70-89
# window reported UNK in Parser Results while the dashboard reported pass, and a run that
# died on a Python traceback or CUDA OOM reported UNK instead of FAIL. The verdict order
# below is the complete one; do not fork it again.
#
# Public:
#   llm_stats_tsv FILE            -> "step_s\ttflops\tstep_std\tperf_std\tmfu" or rc!=0
#   llm_label FILE                -> "STATUS\tstep_s\ttflops\tstep_std\tperf_std\tmfu"
#                                    STATUS is OK | FAIL | UNK
#   nemo_iteration_progress_log F  -> prints "<cur> <tot>" from the last iteration marker
#   nemo_training_finished_log F   -> rc 0 when the run reached its declared target
#   slurm_hard_fail FILE           -> rc 0 on a Slurm-level hard failure
#   nemo_python_fatal_fail FILE    -> rc 0 on a Python/CUDA fatal error
#
# Env:
#   NEMO_TARGET_STEPS      OPT-IN minimum declared target for "finished" (default 0 = accept
#                          whatever the run declared; set it only for formal acceptance gating)
#   NVIS_LLM_PEAK_TFLOPS   per-GPU peak used as the MFU denominator (default 5000)

# MFU denominator. Peak dense TFLOP/s per GPU is a platform property, so it is overridable;
# the default keeps the historical GB200/GB300 number. `perf` is the awk-facing name.
perf="${perf:-${NVIS_LLM_PEAK_TFLOPS:-5000}}"

slurm_hard_fail() {
  grep -E -q 'srun:.*error:.*Exited|error:.*SIGNAL' "$1" 2>/dev/null
}

nemo_python_fatal_fail() {
  grep -E -q \
    'Traceback \(most recent call last\)|torch\.cuda\.OutOfMemoryError|torch\.OutOfMemoryError|CUDA out of memory|CudaOutOfMemoryError' \
    "$1" 2>/dev/null
}

# Megatron lines like "iteration       523/    2010 | ...". Use the LAST such token in the file
# (NCCL noise puts digits on other lines). Prints "<cur> <tot>"; rc!=0 when the log has none.
nemo_iteration_progress_log() {
  local f=$1 last
  last=$(grep -oE 'iteration[[:space:]]+[0-9]+/[[:space:]]*[0-9]+' "$f" 2>/dev/null | tail -1) || true
  [[ -n "$last" ]] || return 1
  [[ "$last" =~ iteration[[:space:]]+([0-9]+)/[[:space:]]*([0-9]+) ]] || return 1
  printf '%s %s' "$((10#${BASH_REMATCH[1]}))" "$((10#${BASH_REMATCH[2]}))"
}

# Finished means the run reached the target IT DECLARED (cur >= tot). A 50-step benchmark and a
# 2010-step acceptance run are both real runs and must both report their numbers.
#
# The rule used to also demand tot >= NEMO_TARGET_STEPS (2010), which made every short run
# permanently "unfinished": job 140457 measured 5.75 s/step at 1432 TFLOP/s/GPU across 576 nodes,
# reached iteration 50/50, and still scored llm_ok=0 — so it never appeared in Charts or
# Analytics. The dashboard's own launch dialog defaults to 50 steps, so the floor rejected the
# most common run shape we produce.
#
# NEMO_TARGET_STEPS is now an OPT-IN acceptance floor: set it to require a minimum declared
# target, leave it unset to accept whatever the run declared.
nemo_training_finished_log() {
  local f=$1 ms="${NEMO_TARGET_STEPS:-0}" prog cur tot
  [[ "$ms" =~ ^[0-9]+$ ]] || ms=0
  prog=$(nemo_iteration_progress_log "$f") || return 1
  cur="${prog%% *}"
  tot="${prog##* }"
  (( tot > 0 && cur >= tot && tot >= ms ))
}

# Averages one extracted sample stream. Step time is field 4 in both dialects; $1 is the field
# carrying TFLOP/s/GPU. Kept as ONE awk program because the two dialects used to carry a copy
# each — the same drift this file exists to prevent.
#
# Sample window:
#   >= 70 samples -> samples 70-89, the historical window, so every number recorded before
#                    short recipes existed stays directly comparable.
#   <  70 samples -> skip a proportional warmup and average the steady-state remainder. A fixed
#                    offset of 70 lands past the end of a 50-step benchmark, which silently
#                    produced no metrics at all: the run reported OK with dashes for every
#                    number and vanished from the analytics scaling tables.
# A short run must still leave MIN_SAMPLES samples after the warmup; a handful of steps from a
# run that died early is noise, and abstaining says so honestly.
_llm_stats_awk() {
  awk -v tf_field="$1" -v perf="$perf" -v min_iter=70 -v max_iter=90 \
      -v warmup_frac=0.2 -v min_samples=5 '
BEGIN { n = 0 }
{ n++; sv[n] = $4; fv[n] = $(tf_field) }
END {
  if (n < 1) exit 1
  if (n >= min_iter) {
    lo = min_iter
    hi = max_iter - 1
    if (hi > n) hi = n
  } else {
    lo = int(n * warmup_frac) + 1
    hi = n
    if (hi - lo + 1 < min_samples) exit 1
  }
  ct = 0; ts = 0; ps = 0
  for (i = lo; i <= hi; i++) { ct++; tv[ct] = sv[i]; ts += sv[i]; pv[ct] = fv[i]; ps += fv[i] }
  if (ct < 1) exit 1
  t_mean = ts / ct
  t_var = 0
  for (i = 1; i <= ct; i++) t_var += (tv[i] - t_mean) ^ 2
  t_std = (ct > 1 ? sqrt(t_var / (ct - 1)) : 0)
  p_mean = ps / ct
  p_var = 0
  for (i = 1; i <= ct; i++) p_var += (pv[i] - p_mean) ^ 2
  p_std = (ct > 1 ? sqrt(p_var / (ct - 1)) : 0)
  mfu = p_mean / perf * 100
  printf "%.2f\t%.2f\t%.5f\t%.5f\t%.1f\n", t_mean, p_mean, t_std, p_std, mfu
}'
}

# Prints: step_s tflops sstd pstd mfu (tab-separated); rc!=0 when there is no valid window.
# Two log dialects: Megatron-Bridge "Step Time … MODEL_TFLOP/s/GPU" and NeMo 2.0
# "train_step_timing in s: …".
llm_stats_tsv() {
  local f=$1
  if grep -q 'Step Time.*MODEL_TFLOP/s/GPU' "$f"; then
    grep -o 'Step Time.*MODEL_TFLOP/s/GPU' "$f" 2>/dev/null | sed -e 's:s GPU: s GPU:g' -e 's:MODEL_TFLOP: MODEL_TFLOP:g' \
      | _llm_stats_awk 8
    return
  fi
  if grep -q 'train_step_timing.*consumed_samples' "$f"; then
    grep -o 'train_step_timing.*consumed_samples' "$f" 2>/dev/null | tr '|' ' ' \
      | _llm_stats_awk 6
    return
  fi
  return 1
}

# Verdict order matters and is deliberate:
#   1. perf stats in the window        -> OK with numbers  (a measured, healthy run)
#   2. reached the declared target      -> OK without numbers (finished, but too few samples
#                                          survived the warmup to average honestly)
#   3. Slurm hard failure               -> FAIL
#   4. Python / CUDA fatal              -> FAIL
#   5. otherwise                        -> UNK (abstain; never guess)
llm_label() {
  local f=$1 stats
  if stats=$(llm_stats_tsv "$f" 2>/dev/null) && [[ -n "$stats" ]]; then
    awk -v OFS='\t' '{ printf "OK\t%s\t%s\t%s\t%s\t%s\n", $1, $2, $3, $4, $5 }' <<<"$stats"
    return
  fi
  if nemo_training_finished_log "$f"; then
    printf 'OK\t-\t-\t-\t-\t-\n'
    return
  fi
  if slurm_hard_fail "$f"; then
    printf 'FAIL\t-\t-\t-\t-\t-\n'
    return
  fi
  if nemo_python_fatal_fail "$f"; then
    printf 'FAIL\t-\t-\t-\t-\t-\n'
    return
  fi
  printf 'UNK\t-\t-\t-\t-\t-\n'
}
