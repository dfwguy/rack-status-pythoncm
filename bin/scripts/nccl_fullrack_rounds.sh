#!/bin/bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# nccl_fullrack_rounds.sh — Stage-1 NCCL acceptance test for GB200/GB300 NVL72 rack validation:
# run N rounds of an all_reduce_perf message-size sweep (default 1K..16G, factor 2) across a
# FULL rack, and report per-round peak busbw + correctness. Wraps the sync-proof portable launcher
# (nccl_portable.sh: NFS OpenMPI + env-injecting rsh agent, survives BCM compute-tray image sync)
# so the MPI wireup + NVLink data path are already correct. Override with NCCL_LAUNCH. Pass criteria:
# every round completes with #wrong == 0 (bandwidth reported for trend/stability review).
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Run on the cluster (a node/head with the nccl workspace on NFS). Launches from the first host.
# Usage: nccl_fullrack_rounds.sh <hosts> [rounds] [beg] [end]
#   <hosts> is EITHER an explicit "h1:4,h2:4,..." (node:gpus) CSV, OR a Slurm nodelist/target
#   (e.g. "bos01-b06-11-...,..." or "p26-r05-ct[01-18]") which is expanded via `scontrol show
#   hostnames` and given GPUS_PER_NODE (default 4) GPUs each.
#   e.g. nccl_fullrack_rounds.sh "$(paste -sd, b05_hosts.txt)" 5 1G 16G
#
# Dashboard integration (so the NVIS Rack Status app discovers + parses the result):
#   NVIS_DASH_LOGDIR : directory to drop the app-discoverable Slurm-style log (default: shared
#                      rack-status log root). File name:
#                        ${USER}-NCCL-1Rack-${NVIS_RACK}-${NNODES}N-<epoch>.txt
#   NVIS_RACK        : rack id used in that filename (the backend passes the canonical rack id).
set -uo pipefail
# Ensure /usr/bin (mpirun/orted) is on PATH — a detached/nohup shell may inherit a minimal PATH.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

# Host NCCL under the cluster's workspace (project setting -> NVIS_WORKSPACE_DIR).
# shellcheck source=workspace-dir-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/workspace-dir-common.sh" || exit 2
NCCL="${NCCL_WS:-${WORKSPACE_DIR}/nccl}"
LAUNCH="${NCCL_LAUNCH:-$NCCL/nccl_portable.sh}"
HOSTS_IN="${1:?usage: nccl_fullrack_rounds.sh <hosts|slurm-nodelist> [rounds] [beg] [end] [rack]}"
ROUNDS="${2:-5}"; BEG="${3:-1K}"; END="${4:-16G}"
# $5 (rack id) is passed positionally by the app because env (NVIS_RACK) is not forwarded over SSH.
RACK_ARG="${5:-}"
GPN="${GPUS_PER_NODE:-4}"
LOG_ROOT="${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-$PWD}}"

# Normalize <hosts> into a "host:GPN,host:GPN,..." CSV. Three cases:
#   (a) already node:gpus CSV (contains ':')            -> use as-is
#   (b) Slurm bracket range e.g. p26-r05-ct[01-18]      -> expand via scontrol (needs slurm on PATH)
#   (c) plain comma list of real hostnames (Boston)     -> split ourselves, no scontrol needed
# Make slurm CLI reachable in non-login SSH shells (Modules not initialized) for case (b).
export PATH="$PATH:/cm/local/apps/slurm/current/bin:/cm/shared/apps/slurm/current/bin:/usr/local/slurm/bin"
_expand_names() {  # emit one hostname per line from a comma list
  printf '%s' "$1" | tr ',' '\n' | sed '/^$/d'
}
if printf '%s' "$HOSTS_IN" | grep -q ':'; then
  HOSTS="$HOSTS_IN"
else
  if printf '%s' "$HOSTS_IN" | grep -q '\['; then
    _names=$(scontrol show hostnames "$HOSTS_IN" 2>/dev/null)
    [ -n "$_names" ] || { echo "ERROR: could not expand bracket nodelist (scontrol missing?): $HOSTS_IN"; exit 2; }
  else
    _names=$(_expand_names "$HOSTS_IN")   # plain comma list -> no scontrol dependency
  fi
  [ -n "$_names" ] || { echo "ERROR: empty nodelist: $HOSTS_IN"; exit 2; }
  HOSTS=$(printf '%s\n' "$_names" | awk -v g="$GPN" 'NF{printf "%s%s:%s", (n++?",":""), $0, g} END{print ""}')
fi

[ -x "$LAUNCH" ] || { echo "ERROR: launcher not found/executable: $LAUNCH"; exit 2; }
NP=$(printf '%s' "$HOSTS" | tr ',' '\n' | awk -F: '{s+=($2==""?1:$2)} END{print s}')
NNODES=$(printf '%s' "$HOSTS" | tr ',' '\n' | grep -c .)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUTDIR="${LOG_ROOT%/}/nccl-rounds/rounds_${STAMP}"
mkdir -p "$OUTDIR"
SUM="$OUTDIR/summary.tsv"
printf 'round\tstatus\tmax_wrong\tpeak_busbw_GB/s\n' > "$SUM"

# App-discoverable log: NCCL-1Rack-<rack> so the single-rack dashboard row + "View Parser Results"
# pick it up (distinct from NEMO). Numeric <epoch> suffix satisfies the app's "…N-<digits>.txt".
RACK="${RACK_ARG:-${NVIS_RACK:-$(printf '%s' "$HOSTS" | cut -d, -f1 | cut -d: -f1)}}"
DASH_DIR="${NVIS_DASH_LOGDIR:-${LOG_ROOT}}"
mkdir -p "$DASH_DIR" 2>/dev/null || true
DLOG="${DASH_DIR}/${USER:-root}-NCCL-1Rack-${RACK}-${NNODES}N-$(date +%s).txt"
{
  echo "NODELIST: $(printf '%s' "$HOSTS" | tr ',' ' ' | sed 's/:[0-9]*//g')"
  echo "=== Stage-1 NCCL acceptance: ${NNODES} nodes / ${NP} GPUs, ${ROUNDS} rounds, ${BEG}..${END} ==="
} | tee "$DLOG"

echo "logs: $OUTDIR ; app log: $DLOG"
fail=0
for r in $(seq 1 "$ROUNDS"); do
  log="$OUTDIR/round_${r}.log"
  HOSTS="$HOSTS" NP="$NP" BEG="$BEG" END="$END" bash "$LAUNCH" > "$log" 2>&1
  # Mirror the full NCCL table into the app log so parser.sh can extract per-size busbw.
  { echo; echo "########## ROUND ${r} ##########"; cat "$log"; } >> "$DLOG"
  # all_reduce_perf data rows: ... busbw_oop #wrong_oop time algbw busbw_ip #wrong_ip
  wrong=$(awk '/^[[:space:]]*[0-9]+[[:space:]]/{a=$(NF)+0; b=$(NF-4)+0; if(a>m)m=a; if(b>m)m=b} END{print m+0}' "$log")
  peak=$(awk '/^[[:space:]]*[0-9]+[[:space:]]/{a=$(NF-1)+0; b=$(NF-5)+0; if(a>p)p=a; if(b>p)p=b} END{printf "%.1f", p+0}' "$log")
  done_ok=$(grep -c 'Avg bus bandwidth' "$log")
  if [ "${done_ok:-0}" -ge 1 ] && [ "${wrong:-1}" -eq 0 ]; then st=PASS; else st=FAIL; fail=1; fi
  printf '%s\t%s\t%s\t%s\n' "$r" "$st" "${wrong:-?}" "${peak:-?}" | tee -a "$SUM"
done

{
  echo; echo "==== SUMMARY ===="
  column -t -s "$(printf '\t')" "$SUM"
  if [ "$fail" -eq 0 ]; then
    echo "RESULT: Stage-1 PASS — ${ROUNDS}/${ROUNDS} rounds clean (${NNODES} nodes / ${NP} GPUs)"
  else
    echo "RESULT: Stage-1 FAIL — see $OUTDIR"
  fi
} | tee -a "$DLOG"
exit "$fail"
