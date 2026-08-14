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
# lib/llmb_autocollect.inc.sh — arm the detached watcher that harvests an llmb run's real output.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Source (do not execute). Shared by run_llmb_nccl.sh and run_llmb_nemo.sh.
#
# Why this exists at all: llmb-run SUBMITS and returns, and the work then runs under nemo_run's
# own `srun --output <experiments>/…/log-default-*.out`, which our sbatch stdout never sees. Left
# alone, the dashboard's canonical log holds a few KB of shell trace and zero iterations, so a
# successful run reports as "not run" / "fail" (jobs 84733-84736, and 127327 which completed
# 50/50 iterations while the UI showed "Stage 2 - NeMo: fail"). The watcher waits for the job to
# leave the queue, then runs the collector that writes the aggregated log the dashboard reads.
#
# Why it is shared: both launchers had their own copy of this block, and the copies had already
# drifted once on the -x/-r gate -- the nccl one was fixed and the nemo one silently kept
# disabling auto-collect on every deployed run, because git tracks the collectors 100644 and the
# watcher invokes them as `bash "$collect"`, which never needs the execute bit.
#
# Public:
#   llmb_jobid_from_output FILE   -> first jobid=<n> or bare sbatch --parsable id, else ""
#   llmb_arm_autocollect TAG COLLECTOR OUT_FILE JOB_LOG_DIR LLMB_INSTALL WATCHER_LOG_BASE \
#                        [COLLECTOR_ARG...]
#       Returns 0 when armed, 1 when skipped (always after an explicit warning). Callers keep
#       their own DRY_RUN gate.
#
# Env:
#   NVIS_LLMB_AUTOCOLLECT=0             disable; run the collector by hand instead
#   RACK_SLURM_SQUEUE                   squeue path override
#   NVIS_LLMB_AUTOCOLLECT_MAX_POLLS     poll cap, default 2880 (~8h at 10s) so a watcher whose
#                                       job vanished cannot linger forever
#   NVIS_LLMB_AUTOCOLLECT_POLL_SEC      queue poll interval, default 10
#   NVIS_LLMB_AUTOCOLLECT_SETTLE_SEC    wait after the job leaves the queue so the last ranks
#                                       flush stdout, default 15

llmb_jobid_from_output() {
  local f="${1:-}"
  [[ -n "$f" && -r "$f" ]] || return 0
  local jid
  jid="$(grep -oE 'jobid=[0-9]+' "$f" 2>/dev/null | head -1 | cut -d= -f2)"
  if [[ -z "$jid" ]]; then
    # Hardened launchers use real `sbatch --parsable --wait`; Slurm emits either
    # `12345` or `12345;cluster`.  Keep this deliberately line-anchored so a
    # node count, scale, timestamp, or result metric can never become a job id.
    jid="$(sed -n -E 's/^[[:space:]]*([0-9]+)(;[^[:space:]]+)?[[:space:]]*$/\1/p' "$f" | head -1)"
  fi
  printf '%s\n' "$jid"
}

llmb_arm_autocollect() {
  local tag="$1" collector="$2" out_file="$3" job_log_dir="$4" llmb_install="$5" watcher_log_base="$6"
  shift 6

  [[ "${NVIS_LLMB_AUTOCOLLECT:-1}" == "1" ]] || return 0

  local jid
  jid="$(llmb_jobid_from_output "$out_file")"
  if [[ -z "$jid" ]]; then
    echo "${tag}: WARNING: could not parse jobid from llmb-run output; auto-collect skipped" >&2
    return 1
  fi

  # Gate on READABLE, not executable: the watcher runs `bash "$collector"`, which does not need
  # the execute bit, so -x tested a permission the invocation never uses and could never pass on
  # a deployed 644 copy. Never skip silently -- a run with no aggregated log is indistinguishable
  # from a run that never happened, and that is how a passing rack gets reported as failed.
  if [[ ! -r "$collector" ]]; then
    echo "${tag}: WARNING: $(basename "$collector") not found or unreadable at ${collector};" \
         "job ${jid} will show as failed until it is run manually" >&2
    return 1
  fi

  echo "${tag}: auto-collect armed for job ${jid} -> ${collector}"
  local squeue max_polls poll_sec settle_sec
  squeue="${RACK_SLURM_SQUEUE:-$(command -v squeue || echo /cm/local/apps/slurm/current/bin/squeue)}"
  max_polls="${NVIS_LLMB_AUTOCOLLECT_MAX_POLLS:-2880}"
  poll_sec="${NVIS_LLMB_AUTOCOLLECT_POLL_SEC:-10}"
  settle_sec="${NVIS_LLMB_AUTOCOLLECT_SETTLE_SEC:-15}"

  # setsid detaches the watcher into its own session so it outlives the SSH session that
  # launched it. It is absent on some hosts (notably macOS), where the previous inline copies
  # printed "armed" and then died with "setsid: command not found" in the watcher log -- the
  # exact silent-skip this include exists to prevent. nohup alone still survives SIGHUP.
  local -a _detach=()
  if command -v setsid >/dev/null 2>&1; then
    _detach=(setsid nohup)
  else
    _detach=(nohup)
  fi

  "${_detach[@]}" bash -c '
    jid="$1"; collect="$2"; squeue="$3"; logdir="$4"; llmb="$5"
    max="$6"; poll="$7"; settle="$8"
    shift 8
    export RACK_JOB_LOG_DIR="$logdir" LLMB_INSTALL="$llmb"
    for _ in $(seq 1 "$max"); do
      "$squeue" -h -j "$jid" -o %i 2>/dev/null | grep -q . || break
      sleep "$poll"
    done
    sleep "$settle"
    bash "$collect" "$jid" "$@"
  ' _ "$jid" "$collector" "$squeue" "$job_log_dir" "$llmb_install" \
    "$max_polls" "$poll_sec" "$settle_sec" "$@" \
    >> "${job_log_dir%/}/${watcher_log_base}_${jid}.log" 2>&1 < /dev/null &
  return 0
}
