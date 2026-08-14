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
# collect_llmb_nccl_log.sh — make an llmb-run NCCL sweep visible to parser.sh + the dashboard.
#
# WHY THIS EXISTS
#   llmb-run writes its Slurm stdout as  llmb-nccl_<label>_<jobid>.out  (progress only:
#   "Running test N/24"), and puts the real nccl-tests tables one-per-test under
#     $LLMB_INSTALL/workloads/microbenchmark_nccl/experiments/*_<jobid>/LOG_*/<test>/rank0_*/stdout.txt
#   parser.sh + nccl_parse.inc.sh instead scan the dashboard log dir for
#     <user>-NCCL-1Rack-<rack>-<N>N-<jobid>.txt
#   and key on "# Collective test starting: <coll>_perf(_mpi)?" blocks -- which llmb's rank0
#   files already contain verbatim. So no parser change is needed: concatenate the rank0
#   stdout files under the expected name and the existing parser reads them as-is.
#
# Usage:  collect_llmb_nccl_log.sh [JOBID]      (default: newest experiment dir)
# Env:    LLMB_INSTALL, RACK_JOB_LOG_DIR / RACK_STATUS_LOG_DIR
set -uo pipefail

LLMB_INSTALL="${LLMB_INSTALL:-/home/cmods/workspace/llmb}"
OUT_DIR="${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-/cluster/benchmark/harish/rack_status/logs}}"
EXP_ROOT="${LLMB_INSTALL%/}/workloads/microbenchmark_nccl/experiments"
JOBID="${1:-}"

die() { echo "collect_llmb_nccl_log: $*" >&2; exit 2; }

mkdir -p "${OUT_DIR}" || die "cannot create output directory: ${OUT_DIR}"
[[ -d "${EXP_ROOT}" ]] || die "no experiments dir: ${EXP_ROOT}"

if [[ -n "${JOBID}" ]]; then
  EXP="$(ls -1d "${EXP_ROOT}"/*_"${JOBID}" 2>/dev/null | head -1)"
  [[ -n "${EXP}" ]] || die "no experiment dir for job ${JOBID} under ${EXP_ROOT}"
else
  EXP="$(ls -1dt "${EXP_ROOT}"/*/ 2>/dev/null | head -1)"
  [[ -n "${EXP}" ]] || die "no experiment dirs under ${EXP_ROOT}"
  JOBID="${EXP%/}"; JOBID="${JOBID##*_}"
fi
EXP="${EXP%/}"

# rank dirs are named rank<N>_<nodename>; derive rack id + node count from them.
mapfile -t NODES < <(find "${EXP}" -maxdepth 3 -type d -name 'rank*_*' -printf '%f\n' 2>/dev/null \
                     | sed 's/^rank[0-9]*_//' | sort -u)
(( ${#NODES[@]} > 0 )) || die "no rank dirs under ${EXP} (job may not have started)"
NNODES="${#NODES[@]}"
# ink50-dh01-h03-28 -> ink50-dh01-h03   (strip the trailing tray number)
RACK="$(printf '%s\n' "${NODES[0]}" | sed -E 's/-[0-9]+$//')"

OUT="${OUT_DIR%/}/${USER:-cmods}-NCCL-1Rack-${RACK}-${NNODES}N-${JOBID}.txt"

# Concatenate rank0 stdout in test order. Test dirs sort as <testset>_<idx>_LOG_<coll>_...
# so a numeric sort on the two leading fields keeps the sweep in execution order.
mapfile -t FILES < <(
  find "${EXP}" -type f -name stdout.txt -path '*/rank0_*' 2>/dev/null \
    | awk -F/ '{ for(i=1;i<=NF;i++) if ($i ~ /_LOG_/) { split($i,a,"_"); print a[1]"\t"a[2]"\t"$0; break } }' \
    | sort -k1,1n -k2,2n | cut -f3-
)
(( ${#FILES[@]} > 0 )) || die "no rank0 stdout.txt with tables under ${EXP}"

{
  echo "# NVIS: aggregated from llmb-run experiment $(basename "${EXP}")"
  echo "# job=${JOBID} rack=${RACK} nodes=${NNODES} tests=${#FILES[@]}"
  for f in "${FILES[@]}"; do
    echo "# --- $(basename "$(dirname "$(dirname "$f")")") ---"
    cat "$f"
  done
} > "${OUT}" || die "cannot write ${OUT}"

echo "collect_llmb_nccl_log: wrote ${OUT}"
echo "collect_llmb_nccl_log: rack=${RACK} nodes=${NNODES} tests=${#FILES[@]} bytes=$(stat -c %s "${OUT}" 2>/dev/null)"
grep -c '^# Collective test starting:' "${OUT}" 2>/dev/null | sed 's/^/collect_llmb_nccl_log: collective blocks=/'
