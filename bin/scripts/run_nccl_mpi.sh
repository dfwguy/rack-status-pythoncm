#!/usr/bin/env bash
# run_nccl_mpi.sh — submit the simple direct-NCCL all_reduce test (nccl_allreduce.sbatch).
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# The lean replacement for the llmb-run NCCL path: resolve NVIS rack ids to Slurm
# nodes, then submit nccl_allreduce.sbatch which runs the HOST nccl-tests
# all_reduce_perf via mpirun (MPI bootstrap ob1/tcp on the routable NS iface, NCCL
# data on the IB rails). No llmb-run, no container, no UCX-verbs guesswork.
#
# Usage:
#   run_nccl_mpi.sh [--dry-run] [--reservation NAME] [--size-max 1G] RACK [RACK ...]
#   run_nccl_mpi.sh [--dry-run] --nodelist '<slurm-nodelist>'
#
# Env: GPUS_PER_NODE (default 4), RACK_TO_NODE_SED / RACK_NODE_MATCH_SUFFIX
#      (see lib/nodes.inc.sh), NVIS_HPCX_DIR / NVIS_NCCL_TESTS_BIN / NVIS_NCCL_LIB /
#      NVIS_CUDA_LIB (host toolchain, auto-discovered on the node when unset),
#      NCCL_SIZE_MIN / NCCL_SIZE_MAX, RACK_JOB_LOG_DIR (defaults to cwd).
set -uo pipefail

JOB_LOG_DIR="${RACK_JOB_LOG_DIR:-$PWD}"
# GPUS_PER_NODE resolved after node resolution (env > Slurm GRES > nvidia-smi > 4).
GPUS_PER_NODE="${GPUS_PER_NODE:-}"
DRY_RUN=0
RESERVATION=""
EXPLICIT_NODELIST=""
SIZE_MAX_OVERRIDE=""
RACKS=()

die() { echo "run_nccl_mpi: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --reservation) RESERVATION="${2:?}"; shift 2 ;;
    --reservation=*) RESERVATION="${1#*=}"; shift ;;
    --nodelist) EXPLICIT_NODELIST="${2:?}"; shift 2 ;;
    --nodelist=*) EXPLICIT_NODELIST="${1#*=}"; shift ;;
    --size-max) SIZE_MAX_OVERRIDE="${2:?}"; shift 2 ;;
    --size-max=*) SIZE_MAX_OVERRIDE="${1#*=}"; shift ;;
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
OUTPUT=""
[[ -n "${JOB_LOG_DIR}" ]] && OUTPUT="${JOB_LOG_DIR%/}/nccl-mpi_${_label}_%j.out"

echo "run_nccl_mpi: nodes=${NVIS_NNODES} nodelist=${NVIS_NODELIST}"
echo "run_nccl_mpi: sbatch=${SBATCH}${OUTPUT:+ output=${OUTPUT}}"

# Export the real scripts dir so the batch script (run from Slurm's spool) can find
# lib/fabric.inc.sh; --export=ALL carries it (+ the submit env) to the job.
export NVIS_SCRIPTS_DIR="${_D}"
cmd=( sbatch -N "${NVIS_NNODES}" --nodelist="${NVIS_NODELIST}" --ntasks-per-node="${GPUS_PER_NODE}"
      --export="ALL,NVIS_SCRIPTS_DIR=${_D},GPUS_PER_NODE=${GPUS_PER_NODE}" )
[[ -n "${OUTPUT}" ]] && cmd+=( --output="${OUTPUT}" )
[[ -n "${RESERVATION}" ]] && cmd+=( --reservation="${RESERVATION}" )
[[ -n "${SIZE_MAX_OVERRIDE}" ]] && export NCCL_SIZE_MAX="${SIZE_MAX_OVERRIDE}"
cmd+=( "${SBATCH}" )

printf 'run_nccl_mpi: exec:'; printf ' %q' "${cmd[@]}"; echo
if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "run_nccl_mpi: --dry-run (not submitting)"
  exit 0
fi
"${cmd[@]}"
