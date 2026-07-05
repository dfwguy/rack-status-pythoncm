#!/usr/bin/env bash
# run_llmb_nccl.sh — NVIS wrapper around dgxc-benchmarking `llmb-run` for the
# NCCL microbenchmark (MOP §7.3). Invokable from a shell or from NVIS Rack Status.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Thin pipeline composed from bin/scripts/lib/ capabilities:
#   nodes.inc.sh    rack ids -> Slurm nodelist + GPU scale
#   fabric.inc.sh   classify NICs -> NCCL_IB_HCA / UCX / NCCL_SOCKET_IFNAME / OMPI_MCA_*
#   sbatch_pin.inc.sh  pin llmb's own sbatch to the nodelist + redirect --output
#
# Usage:
#   run_llmb_nccl.sh [--dry-run] [--reservation NAME] RACK [RACK ...]
#   run_llmb_nccl.sh [--dry-run] --nodelist '<slurm-nodelist>' [--scale N]
#
#   --dry-run            Resolve + print the llmb-run command without submitting (-> -d/--dryrun).
#   --reservation NAME   Pin the Slurm job to a reservation (exported as SBATCH_RESERVATION).
#   --nodelist LIST      Use an explicit Slurm nodelist instead of rack ids.
#   --scale N            Override the computed GPU scale (nodes * GPUS_PER_NODE).
#   --workload NAME      llmb workload (default: microbenchmark_nccl).
#   --model-size NAME    llmb model_size / recipe key (default: nccl; env LLMB_MODEL_SIZE).
#   --dtype TYPE         llmb dtype (default: fp8).
#
# Targets llmb-run v25.10.01 CLI: `llmb-run single -w <workload> -s <model_size>
# --dtype <dtype> --scale <gpus> [-d]`.
#
# Environment (override per site):
#   LLMB_VENV       venv to activate            (default /home/nvis/llmb_venv)
#   LLMB_INSTALL    dgxc-benchmarking WORKSPACE (default /home/nvis/dgxc-benchmarking)
#   LLMB_REPO       dgxc-benchmarking repo dir holding llmb-run (optional; auto-detected)
#   LLMB_RUN        llmb-run binary (optional; resolved from LLMB_REPO/LLMB_INSTALL)
#   GPUS_PER_NODE   GPUs per compute node       (default 4)
#   RACK_TO_NODE_SED / RACK_NODE_MATCH_SUFFIX   rack-id -> node mapping (see lib/nodes.inc.sh)
#   NVIS_RDMA_LINK_TECH  ib|roce|auto — fabric technology (see lib/fabric.inc.sh)
#   NVIS_FABRIC_SKIP=1   skip the representative-node fabric probe (use inherited/overridden env)
#   NCCL_IB_HCA / NCCL_SOCKET_IFNAME / UCX_* / OMPI_MCA_*  — override fabric auto-detect
set -uo pipefail

# NVIS Rack Status launches us with cwd = the dashboard log directory so batch stdout can be
# tailed from the web UI. Capture it BEFORE we cd into LLMB_INSTALL. Override with RACK_JOB_LOG_DIR.
JOB_LOG_DIR="${RACK_JOB_LOG_DIR:-$PWD}"

LLMB_VENV="${LLMB_VENV:-/home/nvis/llmb_venv}"
LLMB_INSTALL="${LLMB_INSTALL:-/home/nvis/dgxc-benchmarking}"
LLMB_INSTALL="${LLMB_INSTALL%/}"
LLMB_REPO="${LLMB_REPO:-}"
LLMB_REPO="${LLMB_REPO%/}"
LLMB_RUN="${LLMB_RUN:-}"
GPUS_PER_NODE="${GPUS_PER_NODE:-4}"
LLMB_WORKLOAD="microbenchmark_nccl"
LLMB_MODEL_SIZE="${LLMB_MODEL_SIZE:-nccl}"
LLMB_DTYPE="fp8"

DRY_RUN=0
RESERVATION=""
EXPLICIT_NODELIST=""
SCALE_OVERRIDE=""
RACKS=()

die() { echo "run_llmb_nccl: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --reservation) RESERVATION="${2:?--reservation needs a value}"; shift 2 ;;
    --reservation=*) RESERVATION="${1#*=}"; shift ;;
    --nodelist) EXPLICIT_NODELIST="${2:?--nodelist needs a value}"; shift 2 ;;
    --nodelist=*) EXPLICIT_NODELIST="${1#*=}"; shift ;;
    --scale) SCALE_OVERRIDE="${2:?--scale needs a value}"; shift 2 ;;
    --scale=*) SCALE_OVERRIDE="${1#*=}"; shift ;;
    --workload) LLMB_WORKLOAD="${2:?}"; shift 2 ;;
    --workload=*) LLMB_WORKLOAD="${1#*=}"; shift ;;
    --model-size) LLMB_MODEL_SIZE="${2:?--model-size needs a value}"; shift 2 ;;
    --model-size=*) LLMB_MODEL_SIZE="${1#*=}"; shift ;;
    --dtype) LLMB_DTYPE="${2:?}"; shift 2 ;;
    --dtype=*) LLMB_DTYPE="${1#*=}"; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --) shift; while [[ $# -gt 0 ]]; do RACKS+=("$1"); shift; done ;;
    -*) die "unknown option: $1" ;;
    *) RACKS+=("$1"); shift ;;
  esac
done

# --- Load Slurm CLI + capability library ----------------------------------------
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
[[ -f "${_SCRIPT_DIR}/slurm-init.inc.sh" ]] && source "${_SCRIPT_DIR}/slurm-init.inc.sh"
# shellcheck disable=SC1091
source "${_SCRIPT_DIR}/lib/nodes.inc.sh"
# shellcheck disable=SC1091
source "${_SCRIPT_DIR}/lib/fabric.inc.sh"
# shellcheck disable=SC1091
source "${_SCRIPT_DIR}/lib/sbatch_pin.inc.sh"
command -v sinfo >/dev/null 2>&1 || die "sinfo not on PATH (Slurm CLI required)"

# --- Resolve nodes + scale ------------------------------------------------------
if [[ -n "${EXPLICIT_NODELIST}" ]]; then
  nvis_nodes_resolve --nodelist "${EXPLICIT_NODELIST}" || exit $?
else
  [[ ${#RACKS[@]} -gt 0 ]] || die "no racks given (pass RACK ids or --nodelist)"
  nvis_nodes_resolve "${RACKS[@]}" || exit $?
fi
NODELIST="${NVIS_NODELIST}"
NNODES="${NVIS_NNODES}"
if [[ -n "${SCALE_OVERRIDE}" ]]; then SCALE="${SCALE_OVERRIDE}"; else SCALE="$(nvis_nodes_scale "${GPUS_PER_NODE}")"; fi

# --- Resolve the llmb-run binary ------------------------------------------------
if [[ -z "${LLMB_RUN}" ]]; then
  _llmb_parent="$(dirname "${LLMB_INSTALL}")"
  for _cand in \
    ${LLMB_REPO:+"${LLMB_REPO}/llmb-run"} \
    "${LLMB_INSTALL}/llmb-run" \
    "${LLMB_INSTALL}/dgxc-benchmarking/llmb-run" \
    "${_llmb_parent}/dgxc-benchmarking/llmb-run"; do
    if [[ -x "${_cand}" || -f "${_cand}" ]]; then LLMB_RUN="${_cand}"; break; fi
  done
fi
if [[ -z "${LLMB_RUN}" || ! ( -x "${LLMB_RUN}" || -f "${LLMB_RUN}" ) ]]; then
  die "llmb-run not found (searched under LLMB_INSTALL=${LLMB_INSTALL}${LLMB_REPO:+ and LLMB_REPO=${LLMB_REPO}}). Set LLMB_INSTALL to the llmb workspace and/or LLMB_REPO to the dgxc-benchmarking repo, or LLMB_RUN to the binary."
fi
# shellcheck disable=SC1091
[[ -f "${LLMB_VENV}/bin/activate" ]] && source "${LLMB_VENV}/bin/activate"
export LLMB_INSTALL

# --- Fabric wireup (NCCL/UCX/OMPI onto the compute rails) -----------------------
# Classify NICs on a representative compute node (homogeneous rack) and export the
# NCCL/UCX/Open MPI env so collectives ride the correct plane cross-rack. Everything
# is overridable: any NCCL_IB_HCA / NCCL_SOCKET_IFNAME / UCX_* set by the app (from
# the platform's tool_param_defaults) or the operator is preserved.
if [[ "${NVIS_FABRIC_SKIP:-0}" != "1" ]]; then
  _first_node="$(nvis_nodes_first)"
  if [[ -n "${_first_node}" ]]; then
    nvis_fabric_export_from_node "${_first_node}" || true
  else
    echo "run_llmb_nccl: WARNING: could not determine a representative node for fabric probe" >&2
  fi
else
  echo "run_llmb_nccl: NVIS_FABRIC_SKIP=1 — using inherited/overridden fabric env" >&2
fi

# --- Pin llmb's own sbatch to the nodelist + redirect --output ------------------
if [[ ${#RACKS[@]} -gt 0 ]]; then _rack_label="$(IFS=+; echo "${RACKS[*]}")"; else _rack_label="nodes"; fi
_rack_label="$(printf '%s' "${_rack_label}" | tr -c 'A-Za-z0-9._+-' '_')"
SBATCH_OUTPUT=""
[[ -n "${JOB_LOG_DIR}" && -d "${JOB_LOG_DIR}" ]] && SBATCH_OUTPUT="${JOB_LOG_DIR%/}/llmb-nccl_${_rack_label}_%j.out"

trap nvis_sbatch_pin_cleanup EXIT
if [[ "${DRY_RUN}" -eq 0 ]]; then
  nvis_sbatch_pin_install "${NODELIST}" "${SBATCH_OUTPUT}"
fi
[[ -n "${RESERVATION}" ]] && export SBATCH_RESERVATION="${RESERVATION}"

# --- Build + run llmb-run -------------------------------------------------------
cmd=( "${LLMB_RUN}" single -w "${LLMB_WORKLOAD}" -s "${LLMB_MODEL_SIZE}" --dtype "${LLMB_DTYPE}" --scale "${SCALE}" )
[[ "${DRY_RUN}" -eq 1 ]] && cmd+=( --dryrun )

echo "run_llmb_nccl: workload=${LLMB_WORKLOAD} model_size=${LLMB_MODEL_SIZE} dtype=${LLMB_DTYPE} nodes=${NNODES} scale=${SCALE}"
echo "run_llmb_nccl: nodelist=${NODELIST} (pinned via sbatch --nodelist shim)${RESERVATION:+ reservation=${RESERVATION}}"
echo "run_llmb_nccl: LLMB_INSTALL=${LLMB_INSTALL} LLMB_RUN=${LLMB_RUN}${NVIS_SHIM_DIR:+ sbatch_shim=${NVIS_SHIM_DIR}/sbatch -> ${LLMB_REAL_SBATCH:-}}"
[[ -n "${SBATCH_OUTPUT}" ]] && echo "run_llmb_nccl: sbatch stdout -> ${SBATCH_OUTPUT} (under dashboard log dir)"
echo "run_llmb_nccl: fabric: NCCL_IB_HCA=${NCCL_IB_HCA:-<auto>} NCCL_SOCKET_IFNAME=${NCCL_SOCKET_IFNAME:-<auto>} UCX_TLS=${UCX_TLS:-<default>} UCX_NET_DEVICES=${UCX_NET_DEVICES:-<auto>}"
printf 'run_llmb_nccl: exec:'; printf ' %q' "${cmd[@]}"; echo

cd "${LLMB_INSTALL}" || die "cannot cd to LLMB_INSTALL=${LLMB_INSTALL}"
"${cmd[@]}"
exit $?
