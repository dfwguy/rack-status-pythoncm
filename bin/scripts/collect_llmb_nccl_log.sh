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
#     <group>-NCCL-1Rack-<rack>-<N>N-<jobid>.txt
#   and key on "# Collective test starting: <coll>_perf(_mpi)?" blocks -- which llmb's rank0
#   files already contain verbatim. So no parser change is needed: concatenate the rank0
#   stdout files under the expected name and the existing parser reads them as-is.
#   <group> is the writer's group (cmods on Nscale NC), not the operator who happened to launch:
#   the file belongs to the cluster's shared log dir. The prefix itself cannot go -- the parser
#   tells the stages apart by "-nccl-1rack-" / "-nemo-1rack-", leading hyphen included.
#
# Usage:  collect_llmb_nccl_log.sh [JOBID [RACK]]   (default: newest experiment dir)
#         RACK is the rack the run was launched against; run_llmb_nccl.sh passes it.
# Env:    LLMB_INSTALL, RACK_JOB_LOG_DIR / RACK_STATUS_LOG_DIR, NVIS_RACK (RACK when not given)
set -uo pipefail

# LLMB_INSTALL under the cluster's workspace (project setting -> NVIS_WORKSPACE_DIR).
# shellcheck source=lib/llmb_paths.inc.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/llmb_paths.inc.sh" || exit 2
OUT_DIR="${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-$PWD}}"
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
# The parser finds this file by the rack id the run was launched against, so that id names it:
# the launcher passes it as $2 (the backend exports it as NVIS_RACK). Only without it is the rack
# read off a hostname, which works where the name carries the rack (ink50-dh01-h03-28 ->
# ink50-dh01-h03, the trailing tray stripped) and not where it does not: on Nscale NC a node is
# sys1-gso1-p-phy-gpu55-su19 and its rack (U22) lives only in BCM, so the guess named U22's Stage-1
# result after a node and the parser never found it. A value that is not a plain file-name token
# is ignored rather than put in a path.
_safe_token() { [[ "$1" =~ ^[A-Za-z0-9._-]+$ && "$1" != *..* ]]; }
if _safe_token "${2:-}"; then
  RACK="$2"
elif _safe_token "${NVIS_RACK:-}"; then
  RACK="${NVIS_RACK}"
else
  RACK="$(printf '%s\n' "${NODES[0]}" | sed -E 's/-[0-9]+$//')"
fi
OWNER="$(id -gn 2>/dev/null)"
_safe_token "${OWNER}" || OWNER=cmods

OUT="${OUT_DIR%/}/${OWNER}-NCCL-1Rack-${RACK}-${NNODES}N-${JOBID}.txt"

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
