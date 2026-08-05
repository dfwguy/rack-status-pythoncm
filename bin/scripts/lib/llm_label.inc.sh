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
#   nemo_training_finished_log F   -> rc 0 when the run reached its declared target
#   slurm_hard_fail FILE           -> rc 0 on a Slurm-level hard failure
#   nemo_python_fatal_fail FILE    -> rc 0 on a Python/CUDA fatal error
#
# Env:
#   NEMO_TARGET_STEPS      minimum DECLARED total steps for "finished" (default 2010)
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
# (NCCL noise puts digits on other lines). Finished when cur>=tot AND tot>=NEMO_TARGET_STEPS:
# the first half rejects a truncated run, the second rejects a run whose declared target was
# too short to count as acceptance.
nemo_training_finished_log() {
  local f=$1 ms="${NEMO_TARGET_STEPS:-2010}" last cur tot
  [[ "$ms" =~ ^[0-9]+$ ]] || ms=2010
  last=$(grep -oE 'iteration[[:space:]]+[0-9]+/[[:space:]]*[0-9]+' "$f" 2>/dev/null | tail -1) || true
  [[ -n "$last" ]] || return 1
  [[ "$last" =~ iteration[[:space:]]+([0-9]+)/[[:space:]]*([0-9]+) ]] || return 1
  cur=$((10#${BASH_REMATCH[1]}))
  tot=$((10#${BASH_REMATCH[2]}))
  (( cur >= tot && tot >= ms ))
}

# Prints: step_s tflops sstd pstd mfu (tab-separated); rc!=0 when there is no valid window.
# Two log dialects: Megatron-Bridge "Step Time … MODEL_TFLOP/s/GPU" and NeMo 2.0
# "train_step_timing in s: …". Window is lines 70-89 of the extracted stream, i.e. past warmup.
llm_stats_tsv() {
  local f=$1
  if grep -q 'Step Time.*MODEL_TFLOP/s/GPU' "$f"; then
    grep -o 'Step Time.*MODEL_TFLOP/s/GPU' "$f" 2>/dev/null | sed -e 's:s GPU: s GPU:g' -e 's:MODEL_TFLOP: MODEL_TFLOP:g' \
      | awk -v min_iter=70 -v max_iter=90 -v perf="$perf" '
BEGIN { ct = 0; ts = 0; ps = 0 }
(min_iter <= NR && NR < max_iter) { ct++; tv[ct] = $4; ts += $4; pv[ct] = $8; ps += $8 }
END {
  if (ct < 1) exit 1
  t_mean = ts / ct
  t_var = 0
  for (i = 1; i <= ct; i++) t_var += (tv[i] - t_mean) ^ 2
  t_std = (ct > 1 ? sqrt(t_var / (ct - 1)) : 0)
  p_mean = ps / ct
  p_var = 0
  for (i = 1; i <= ct; i++) p_var += (pv[i] - p_mean) ^ 2
  p_std = (ct > 1 ? sqrt(p_var / (ct - 1)) : 0)
  mfu = ps / ct / perf * 100
  printf "%.2f\t%.2f\t%.5f\t%.5f\t%.1f\n", ts / ct, ps / ct, t_std, p_std, mfu
}'
    return 0
  fi
  if grep -q 'train_step_timing.*consumed_samples' "$f"; then
    grep -o 'train_step_timing.*consumed_samples' "$f" 2>/dev/null | tr '|' ' ' \
      | awk -v min_iter=70 -v max_iter=90 -v perf="$perf" '
BEGIN { ct = 0; ts = 0; ps = 0 }
(min_iter <= NR && NR < max_iter) { ct++; tv[ct] = $4; ts += $4; pv[ct] = $6; ps += $6 }
END {
  if (ct < 1) exit 1
  t_mean = ts / ct
  t_var = 0
  for (i = 1; i <= ct; i++) t_var += (tv[i] - t_mean) ^ 2
  t_std = (ct > 1 ? sqrt(t_var / (ct - 1)) : 0)
  p_mean = ps / ct
  p_var = 0
  for (i = 1; i <= ct; i++) p_var += (pv[i] - p_mean) ^ 2
  p_std = (ct > 1 ? sqrt(p_var / (ct - 1)) : 0)
  mfu = ps / ct / perf * 100
  printf "%.2f\t%.2f\t%.5f\t%.5f\t%.1f\n", ts / ct, ps / ct, t_std, p_std, mfu
}'
    return 0
  fi
  return 1
}

# Verdict order matters and is deliberate:
#   1. perf stats in the window        -> OK with numbers  (a measured, healthy run)
#   2. reached the declared target      -> OK without numbers (finished; window may be absent
#                                          for short recipes such as a 50-step benchmark)
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
