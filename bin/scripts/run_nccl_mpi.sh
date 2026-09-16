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
# run_nccl_mpi.sh — submit the simple direct-NCCL all_reduce test (nccl_allreduce.sbatch).
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# The lean replacement for the llmb-run NCCL path: resolve NVIS rack ids to Slurm
# nodes, then submit nccl_allreduce.sbatch which runs the HOST nccl-tests
# all_reduce_perf via mpirun (MPI bootstrap ob1/tcp on the routable NS iface, NCCL
# data on the IB rails). No llmb-run, no container, no UCX-verbs guesswork.
#
# Usage:
#   run_nccl_mpi.sh [--dry-run] [--reservation NAME] [--size-max 1G] [--nccl-version 2.30.7] \
#                   [--rounds 5] [--app-log-rack RACK] RACK [RACK ...]
#   run_nccl_mpi.sh [--dry-run] --nodelist '<slurm-nodelist>'
#
# --rounds N           : run the all_reduce sweep N times in one job (Stage-1 acceptance = 5).
#                        Forwarded to the sbatch as NVIS_NCCL_ROUNDS. Default 1.
#
# --app-log-rack RACK  : name the Slurm output "<user>-NCCL-1Rack-<RACK>-<N>N-<jobid>.txt" so the
#                        app captures it as the single-rack Stage-1 NCCL validation (dashboard +
#                        parser). Without it, the log is "nccl-mpi_<label>_<jobid>.out" (x-rack style).
#
# Env: GPUS_PER_NODE (default 4), RACK_TO_NODE_SED / RACK_NODE_MATCH_SUFFIX
#      (rack->node mapping; auto-detected per site when unset — see lib/nodes.inc.sh),
#      NVIS_HPCX_DIR / NVIS_NCCL_TESTS_BIN / NVIS_NCCL_LIB /
#      NVIS_CUDA_LIB (host toolchain, auto-discovered on the node when unset),
#      NVIS_NCCL_VERSION (which vetted libnccl to load: 2.30.7 default | system | <X.Y.Z>),
#      NCCL_SIZE_MIN / NCCL_SIZE_MAX, RACK_JOB_LOG_DIR / RACK_STATUS_LOG_DIR.
set -uo pipefail

JOB_LOG_DIR="${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-/cluster/benchmark/harish/rack_status/logs}}"
# GPUS_PER_NODE resolved after node resolution (env > Slurm GRES > nvidia-smi > 4).
GPUS_PER_NODE="${GPUS_PER_NODE:-}"
DRY_RUN=0
RESERVATION=""
EXPLICIT_NODELIST=""
SIZE_MAX_OVERRIDE=""
NCCL_VERSION_OVERRIDE=""
ROUNDS_OVERRIDE=""
APP_LOG_RACK=""
RACKS=()

die() { echo "run_nccl_mpi: $*" >&2; exit 2; }

mkdir -p "${JOB_LOG_DIR}" || die "cannot create log directory: ${JOB_LOG_DIR}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --reservation) RESERVATION="${2:?}"; shift 2 ;;
    --reservation=*) RESERVATION="${1#*=}"; shift ;;
    --nodelist) EXPLICIT_NODELIST="${2:?}"; shift 2 ;;
    --nodelist=*) EXPLICIT_NODELIST="${1#*=}"; shift ;;
    --size-max) SIZE_MAX_OVERRIDE="${2:?}"; shift 2 ;;
    --size-max=*) SIZE_MAX_OVERRIDE="${1#*=}"; shift ;;
    --nccl-version) NCCL_VERSION_OVERRIDE="${2:?}"; shift 2 ;;
    --nccl-version=*) NCCL_VERSION_OVERRIDE="${1#*=}"; shift ;;
    --rounds) ROUNDS_OVERRIDE="${2:?}"; shift 2 ;;
    --rounds=*) ROUNDS_OVERRIDE="${1#*=}"; shift ;;
    --app-log-rack) APP_LOG_RACK="${2:?}"; shift 2 ;;
    --app-log-rack=*) APP_LOG_RACK="${1#*=}"; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --) shift; while [[ $# -gt 0 ]]; do RACKS+=("$1"); shift; done ;;
    -*) die "unknown option: $1" ;;
    *) RACKS+=("$1"); shift ;;
  esac
done

_D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
[[ -f "${_D}/slurm-init.inc.sh" ]] && source "${_D}/slurm-init.inc.sh"
# shellcheck disable=SC1091
source "${_D}/lib/nodes.inc.sh"
command -v sinfo >/dev/null 2>&1 || die "sinfo not on PATH (Slurm CLI required)"
command -v sbatch >/dev/null 2>&1 || die "sbatch not on PATH"

# --- Resolve nodes --------------------------------------------------------------
if [[ -n "${EXPLICIT_NODELIST}" ]]; then
  nvis_nodes_resolve --nodelist "${EXPLICIT_NODELIST}" || exit $?
  _label="nodes"
else
  [[ ${#RACKS[@]} -gt 0 ]] || die "no racks given (pass RACK ids or --nodelist)"
  nvis_nodes_resolve "${RACKS[@]}" || exit $?
  _label="$(IFS=+; echo "${RACKS[*]}")"
fi
_label="$(printf '%s' "${_label}" | tr -c 'A-Za-z0-9._+-' '_')"

# GPUs/node: explicit env wins; else detect from Slurm GRES of the first node
# (platform-agnostic: NVL=4, HGX=8, etc.); else the sbatch's on-node nvidia-smi
# takes over (default 4). This keeps --ntasks-per-node consistent with the node.
if [[ -z "${GPUS_PER_NODE}" ]]; then
  GPUS_PER_NODE="$(nvis_nodes_gpus_per_node "$(nvis_nodes_first)")"
fi
GPUS_PER_NODE="${GPUS_PER_NODE:-4}"
echo "run_nccl_mpi: gpus/node=${GPUS_PER_NODE}"

SBATCH="${_D}/nccl_allreduce.sbatch"
[[ -f "${SBATCH}" ]] || die "nccl_allreduce.sbatch not found next to this launcher"
IMEX_BATCH="${_D}/validation_imex_channel0_prepare.sbatch"
[[ -f "${IMEX_BATCH}" ]] || die "validation_imex_channel0_prepare.sbatch not found next to this launcher"
OUTPUT=""
if [[ -n "${JOB_LOG_DIR}" ]]; then
  if [[ -n "${APP_LOG_RACK}" ]]; then
    # App-discoverable single-rack NCCL log: the dashboard + parser key off the
    # "…-NCCL-1Rack-<rack>-<N>N-…txt" basename to flip the rack's Stage-1 NCCL status.
    OUTPUT="${JOB_LOG_DIR%/}/${USER:-root}-NCCL-1Rack-${APP_LOG_RACK}-${NVIS_NNODES}N-%j.txt"
  else
    OUTPUT="${JOB_LOG_DIR%/}/nccl-mpi_${_label}_%j.out"
  fi
fi

echo "run_nccl_mpi: nodes=${NVIS_NNODES} nodelist=${NVIS_NODELIST}"
echo "run_nccl_mpi: sbatch=${SBATCH}${OUTPUT:+ output=${OUTPUT}}"

# Export the real scripts dir so the batch script (run from Slurm's spool) can find
# lib/fabric.inc.sh; --export=ALL carries it (+ the submit env) to the job.
export NVIS_SCRIPTS_DIR="${_D}"
# NCCL version: --nccl-version wins; else inherit NVIS_NCCL_VERSION from the env; else
# the sbatch default (2.30.7). Forwarded explicitly so the batch script picks the right
# vetted libnccl (loaded via LD_LIBRARY_PATH from the NFS workspace).
[[ -n "${NCCL_VERSION_OVERRIDE}" ]] && export NVIS_NCCL_VERSION="${NCCL_VERSION_OVERRIDE}"
# Sweep rounds: --rounds wins; else inherit NVIS_NCCL_ROUNDS from the env; else sbatch default (1).
[[ -n "${ROUNDS_OVERRIDE}" ]] && export NVIS_NCCL_ROUNDS="${ROUNDS_OVERRIDE}"
_export="ALL,NVIS_SCRIPTS_DIR=${_D},GPUS_PER_NODE=${GPUS_PER_NODE}"
[[ -n "${NVIS_NCCL_VERSION:-}" ]] && _export="${_export},NVIS_NCCL_VERSION=${NVIS_NCCL_VERSION}"
[[ -n "${NVIS_NCCL_ROUNDS:-}" ]] && _export="${_export},NVIS_NCCL_ROUNDS=${NVIS_NCCL_ROUNDS}"
[[ -n "${NVIS_NCCL_RUNTIME_PREFLIGHT:-}" ]] && _export="${_export},NVIS_NCCL_RUNTIME_PREFLIGHT=${NVIS_NCCL_RUNTIME_PREFLIGHT}"
[[ -n "${NVIS_NCCL_MIN_CUDA_VERSION:-}" ]] && _export="${_export},NVIS_NCCL_MIN_CUDA_VERSION=${NVIS_NCCL_MIN_CUDA_VERSION}"
[[ -n "${NVIS_CUDA_LIB:-}" ]] && _export="${_export},NVIS_CUDA_LIB=${NVIS_CUDA_LIB}"
cmd=( sbatch -N "${NVIS_NNODES}" --nodelist="${NVIS_NODELIST}" --ntasks-per-node="${GPUS_PER_NODE}"
      --export="${_export}" )
[[ "${NVIS_NCCL_SBATCH_WAIT:-0}" == 1 ]] && cmd+=( --wait --no-requeue )
[[ -n "${OUTPUT}" ]] && cmd+=( --output="${OUTPUT}" )
[[ -n "${RESERVATION}" ]] && cmd+=( --reservation="${RESERVATION}" )
[[ -n "${SIZE_MAX_OVERRIDE}" ]] && export NCCL_SIZE_MAX="${SIZE_MAX_OVERRIDE}"
cmd+=( "${SBATCH}" )

imex_output="${JOB_LOG_DIR%/}/validation-imex-channel0-%j.out"
imex_cmd=( sbatch --parsable --wait --no-requeue -N "${NVIS_NNODES}"
           --nodelist="${NVIS_NODELIST}" --ntasks-per-node=1
           --output="${imex_output}" "${IMEX_BATCH}" )

printf 'run_nccl_mpi: imex prelaunch:'; printf ' %q' "${imex_cmd[@]}"; echo
printf 'run_nccl_mpi: exec:'; printf ' %q' "${cmd[@]}"; echo
if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "run_nccl_mpi: --dry-run (not submitting)"
  exit 0
fi

imex_reply="$("${imex_cmd[@]}" 2>&1)"
imex_rc=$?
printf '%s\n' "$imex_reply"
imex_id="$(printf '%s\n' "$imex_reply" | sed -n 's/^\([0-9][0-9]*\)\(;.*\)\{0,1\}$/\1/p' | head -1)"
[[ "$imex_id" =~ ^[0-9]+$ ]] || die "IMEX prelaunch returned no valid Slurm id (rc=${imex_rc}): ${imex_reply}"
(( imex_rc == 0 )) || die "IMEX prelaunch failed (job=${imex_id} rc=${imex_rc} log=${imex_output//%j/$imex_id})"
echo "run_nccl_mpi: IMEX channel0 ready on ${NVIS_NNODES}/${NVIS_NNODES} node(s) (job=${imex_id} log=${imex_output//%j/$imex_id})"
"${cmd[@]}"
