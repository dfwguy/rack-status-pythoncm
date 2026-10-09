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
# collect_llmb_nemo_log.sh <jobid> — make the llmb/NEMO training output visible to
# the dashboard, parser and lake.
#
# Why this is needed (and why run_llmb_nemo.sh's sbatch --output is NOT enough):
# llmb-run hands off to nemo_run, whose generated batch script runs the real work via
#     srun --output <experiments>/.../log-default-default.<name>_%j_0.out ...
# That INNER --output is baked into the generated script, so our sbatch shim (which
# rewrites the OUTER batch --output) never sees it. Result: the canonical
# <user>-NEMO-1Rack-<rack>-<N>N-<jobid>.txt gets ~6 KB of shell trace and ZERO
# iterations, while the 3.8 MB of real output sits in the experiment dir where
# parser.sh cannot find it. Jobs 84733-84736 all completed successfully and showed
# as "not run" in the GUI for exactly this reason.
#
# NCCL has the same shape of problem and solves it with collect_llmb_nccl_log.sh;
# this is the NEMO equivalent.
#
# Idempotent: a marker line is written, and a second run is a no-op. That matters
# because the periodic /api/universal/scan re-reads these files.
#
# Usage: collect_llmb_nemo_log.sh <jobid> [rack]
set -uo pipefail

JID="${1:-}"
RACK="${2:-}"
[[ -n "${JID}" ]] || { echo "usage: $0 <jobid> [rack]" >&2; exit 2; }

LOG_DIR="${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-$PWD}}"
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKER="### NVIS: llmb inner log for job ${JID} appended below ###"

mkdir -p "${LOG_DIR}" || { echo "collect_llmb_nemo: cannot create log directory: ${LOG_DIR}" >&2; exit 2; }

# The canonical file the dashboard/parser key on. Prefer an existing one (written by
# the sbatch shim); otherwise synthesise the name, which needs the rack.
canon="$(ls -1 "${LOG_DIR}"/*NEMO-1Rack-*-"${JID}".txt 2>/dev/null | head -1)"
if [[ -z "${canon}" ]]; then
  [[ -n "${RACK}" ]] || { echo "collect_llmb_nemo: no canonical log for ${JID} and no rack given" >&2; exit 4; }
  nodes="$(grep -oE 'NumNodes=[0-9]+' <<<"$(scontrol show job "${JID}" 2>/dev/null)" | head -1 | cut -d= -f2)"
  canon="${LOG_DIR%/}/${USER:-cmods}-NEMO-1Rack-${RACK}-${nodes:-18}N-${JID}.txt"
  : > "${canon}"
fi

if grep -qF "${MARKER}" "${canon}" 2>/dev/null; then
  echo "collect_llmb_nemo: ${canon} already collected (no-op)"
  exit 0
fi

# The generated NeMo batch script records the exact inner output path in the
# canonical shell trace.  That path is authoritative: LLMB_INSTALL is the code
# checkout, while sites may independently relocate experiment data (as happened
# when runs moved to /cluster/benchmark/... and every watcher searched the old
# $LLMB_INSTALL tree).  Only accept a trace target whose basename
# carries this exact job id.
inner=""
while IFS= read -r candidate; do
  candidate="${candidate#\'}"
  candidate="${candidate%\'}"
  candidate="${candidate#\"}"
  candidate="${candidate%\"}"
  # xtrace records this command before Slurm expands filename tokens, so the
  # authoritative path commonly still contains literal %j. Resolve only the
  # job-id token we know from the canonical filename/watcher argument.
  candidate="${candidate//%j/${JID}}"
  base="${candidate##*/}"
  if [[ "${base}" == *_"${JID}"_*.out && -f "${candidate}" ]]; then
    inner="${candidate}"
  fi
done < <(
  awk '
    /^\+ / && /srun/ {
      for (i = 1; i <= NF; i++) {
        if ($i == "--output" && i < NF) print $(i + 1)
        else if ($i ~ /^--output=/) { sub(/^--output=/, "", $i); print $i }
      }
    }
  ' "${canon}" 2>/dev/null
)

searched=()
if [[ -z "${inner}" ]]; then
  search_roots=()
  if [[ -n "${LLMB_EXPERIMENTS_ROOT:-}" ]]; then
    search_roots+=("${LLMB_EXPERIMENTS_ROOT%/}")
    search_roots+=("${LLMB_EXPERIMENTS_ROOT%/}/workloads/pretrain_nemotron-h/experiments")
  fi
  # Last resort, and the only use of LLMB_INSTALL here: resolved lazily (lib/llmb_paths.inc.sh --
  # explicit, else <cluster workspace>/llmb) so a re-run that is already collected, or a trace
  # that names its output, never needs an LLMB location at all.
  # shellcheck source=lib/llmb_paths.inc.sh
  if . "${_SCRIPT_DIR}/lib/llmb_paths.inc.sh" 2>/dev/null; then
    search_roots+=("${LLMB_INSTALL%/}/workloads/pretrain_nemotron-h/experiments")
  fi
  for root in "${search_roots[@]}"; do
    [[ -n "${root}" ]] || continue
    searched+=("${root}")
    candidate="$(find "${root}" -name "log-default-default*_${JID}_*.out" 2>/dev/null | head -1)"
    if [[ -n "${candidate}" && -f "${candidate}" ]]; then
      inner="${candidate}"
      break
    fi
  done
fi

if [[ -z "${inner}" || ! -f "${inner}" ]]; then
  echo "collect_llmb_nemo: no inner log for job ${JID}" >&2
  echo "  canonical trace: ${canon}" >&2
  echo "  fallback roots : ${searched[*]:-(none)}" >&2
  exit 3
fi

{
  printf '\n%s\n' "${MARKER}"
  printf '### source: %s\n\n' "${inner}"
  cat "${inner}"
} >> "${canon}"

echo "collect_llmb_nemo: job=${JID}"
echo "  inner : ${inner} ($(du -h "${inner}" 2>/dev/null | cut -f1))"
echo "  canon : ${canon} ($(du -h "${canon}" 2>/dev/null | cut -f1))"
echo "  iters : $(grep -c 'elapsed time per iteration' "${canon}" 2>/dev/null)"
echo "  tflops: $(grep -oE '[0-9.]+MODEL_TFLOP/s/GPU' "${canon}" 2>/dev/null | tail -1)"
