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

# Keep dashboard and manual launches on the shared cluster log root. RACK_JOB_LOG_DIR
# remains the per-launch override; RACK_STATUS_LOG_DIR remains the deployment override.
JOB_LOG_DIR="${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-/cluster/benchmark/harish/rack_status/logs}}"

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

mkdir -p "${JOB_LOG_DIR}" || die "cannot create log directory: ${JOB_LOG_DIR}"

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
# shellcheck disable=SC1091
source "${_SCRIPT_DIR}/lib/llmb_autocollect.inc.sh"
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

# Materialize the GB300 IMEX channel on the exact reviewed allocation before
# llmb-run can submit its NCCL workload.  Keep this identical to the direct and
# containerized Rack Status launchers: derive the registered major locally on
# every node, fail closed if validation/open fails, and install no persistent
# service, timer, udev rule, or reconciler.
IMEX_BATCH="${_SCRIPT_DIR}/validation_imex_channel0_prepare.sbatch"
[[ -r "${IMEX_BATCH}" ]] || die "IMEX channel0 prelaunch batch is missing: ${IMEX_BATCH}"
imex_output="${JOB_LOG_DIR%/}/validation-imex-channel0-%j.out"
imex_cmd=(sbatch --parsable --wait --no-requeue -N "${NNODES}"
  --nodelist="${NODELIST}" --ntasks-per-node=1 --output="${imex_output}")
[[ -n "${RESERVATION}" ]] && imex_cmd+=(--reservation="${RESERVATION}")
imex_cmd+=("${IMEX_BATCH}")
printf 'run_llmb_nccl: imex prelaunch:'; printf ' %q' "${imex_cmd[@]}"; echo
if [[ "${DRY_RUN}" -eq 0 ]]; then
  imex_reply="$("${imex_cmd[@]}" 2>&1)"
  imex_rc=$?
  printf '%s\n' "${imex_reply}"
  imex_id="$(printf '%s\n' "${imex_reply}" | sed -n 's/^\([0-9][0-9]*\)\(;.*\)\{0,1\}$/\1/p' | head -1)"
  [[ "${imex_id}" =~ ^[0-9]+$ ]] || die "IMEX prelaunch returned no valid Slurm id (rc=${imex_rc}): ${imex_reply}"
  (( imex_rc == 0 )) || die "IMEX prelaunch failed (job=${imex_id} rc=${imex_rc} log=${imex_output//%j/${imex_id}})"
  echo "run_llmb_nccl: IMEX channel0 ready on ${NNODES}/${NNODES} node(s) (job=${imex_id} log=${imex_output//%j/${imex_id}})"
fi

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

# --- Container reality check: drop host-only UCX pins ---------------------------
# llmb-run executes inside the NeMo pyxis/enroot container, which exposes only
# tcp/IPoIB netdevs -- no mlx5 verbs devices. The fabric probe above is written for the
# HOST mpirun path; two of its exports are fatal in-container:
#   UCX_TLS=^tcp        forbids the only transport the container actually has, so UCX
#                       reports "no active messages transport" and every rank is Killed.
#   UCX_NET_DEVICES=mlx5_*:1  names devices that do not exist inside the container.
# Job 65827 died this way on EVERY tray, including 4-rail ones -- it is a transport
# problem, not a rail-count problem. MPI is only the bootstrap here (NCCL does the
# collectives over IB directly), so letting UCX use tcp costs nothing.
# Set NVIS_LLMB_KEEP_UCX_ENV=1 to keep the host pins (host/non-container runs only).
if [[ "${NVIS_LLMB_KEEP_UCX_ENV:-0}" != "1" ]]; then
  for _v in UCX_TLS UCX_NET_DEVICES; do
    if [[ -n "${!_v:-}" ]]; then
      echo "run_llmb_nccl: unsetting ${_v}=${!_v} (host-only pin; fatal inside the llmb container)" >&2
      unset "${_v}"
    fi
  done
fi

# --- Apply the operator's NCCL debug level to the workload's testset config ------
# THE JOB ENVIRONMENT DOES NOT REACH THE RANKS ON THIS PATH. The workload is NVIDIA
# Comms-Perf-Suite: its templates/nccl.sbatch.j2 emits `--container-env=NCCL_DEBUG=<v>`, an
# EXPLICIT pyxis assignment built from testset.toml -- so whatever NCCL_DEBUG is in the job env is
# ignored. Measured 2026-08-04: job stdout carries 0 NCCL lines on this path (vs 30,942 on the
# host-mpirun path, where nccl_allreduce.sbatch does `mpirun -x NCCL_DEBUG`), and the per-rank
# nccl_debug_file.* show the TESTSET's level, never ours. Forcing it through sbatch --export
# cannot work; the level has to be written into the testset the run actually uses.
#
# Rack Status sends the GUI's nccl_debug as NCCL_DEBUG + NVIS_NCCL_DEBUG (env_extra), so it is in
# THIS shell. Rewrite the testset's existing NCCL_DEBUG assignment to match.
# SAFETY: only ever REWRITES AN ASSIGNMENT THAT ALREADY EXISTS -- never invents structure, never
# creates the key, never touches any other line. Backs the file up once. If nothing matches it
# says so loudly and changes nothing, because a silent no-op here is what wasted a whole session.
# Disable with NVIS_LLMB_TESTSET_APPLY=0. Point at a specific file with NVIS_LLMB_TESTSET=<path>.
_dbg="${NCCL_DEBUG:-${NVIS_NCCL_DEBUG:-}}"
case "${_dbg}" in VERSION|WARN|INFO|TRACE) ;; *) _dbg="" ;; esac
if [[ -n "${_dbg}" && "${NVIS_LLMB_TESTSET_APPLY:-1}" == "1" ]]; then
  # There are MULTIPLE testset.toml copies on a real install (measured 2026-08-04: LLMB_REPO's,
  # LLMB_INSTALL/llmb_repo's, and the workload's Nvidia-Comms-Perf-Suite one). Editing only the
  # first match silently edited a copy the run does not read — job 114325 was launched with
  # NCCL_DEBUG=TRACE written to the LLMB_REPO copy while its own test_set_metadata.yml recorded
  # NCCL_DEBUG: "WARN". Which copy llmb-run consumes is not documented, so apply to ALL of them.
  # PROVEN 2026-08-04 (jobs 114324/114325/114326): the effective level lives in **systems.toml**,
  # NOT testset.toml. systems.toml has per-GPU-type sections ([gb300.run_params] etc.) each holding
  # a LIST of "KEY=VALUE" strings:      "NCCL_DEBUG=WARN",
  # Three runs were burned editing testset.toml — each job's own test_set_metadata.yml kept
  # reporting NCCL_DEBUG: "WARN", which is the artifact that proved the edit never landed.
  # ALWAYS verify against test_set_metadata.yml, never against "the file I wrote looks right".
  _tslist=()
  if [[ -n "${NVIS_LLMB_TESTSET:-}" ]]; then
    _tslist=( "${NVIS_LLMB_TESTSET}" )
  else
    while IFS= read -r _c; do
      [[ -f "${_c}" ]] && grep -q '"NCCL_DEBUG=' "${_c}" 2>/dev/null && _tslist+=( "${_c}" )
    done < <(
      { [[ -n "${LLMB_REPO}" ]] && find "${LLMB_REPO}" -maxdepth 6 -name 'systems.toml' \
            -not -path '*example*' -not -path '*/tests/*' 2>/dev/null
        find "${LLMB_INSTALL%/}" -maxdepth 7 -name 'systems.toml' \
            -not -path '*example*' -not -path '*/tests/*' -not -path '*/.cache/*' 2>/dev/null
      } | sort -u
    )
  fi
  echo "run_llmb_nccl: testset candidates: ${#_tslist[@]}${_tslist[*]:+ -> ${_tslist[*]}}"
  if [[ ${#_tslist[@]} -gt 0 ]]; then
   for _ts in "${_tslist[@]}"; do
    _old="$(grep -m1 -o '"NCCL_DEBUG=[^"]*"' "${_ts}")"
    [[ -f "${_ts}.nvis-orig" ]] || cp -p "${_ts}" "${_ts}.nvis-orig" 2>/dev/null
    if sed -i -E "s|\"NCCL_DEBUG=[^\"]*\"|\"NCCL_DEBUG=${_dbg}\"|g" "${_ts}" 2>/dev/null; then
      echo "run_llmb_nccl: NCCL debug -> ${_dbg} in ${_ts} (was: ${_old:-<none>}; backup ${_ts}.nvis-orig)"
    else
      echo "run_llmb_nccl: WARNING: could not rewrite NCCL_DEBUG in ${_ts}; ranks will use its existing level" >&2
    fi
    # NCCL_DEBUG_SUBSYS gates WHICH subsystems emit at the chosen level. Measured 2026-08-04 on
    # job 114324: with NCCL_DEBUG=TRACE but no SUBSYS, the window around the failure is still all
    # "NCCL INFO" — TRACE alone bought nothing, so the failing CUDA call stayed invisible. TRACE
    # without SUBSYS is a wasted run. Default to ALL for TRACE; override with NVIS_NCCL_DEBUG_SUBSYS.
    _sub="${NVIS_NCCL_DEBUG_SUBSYS:-}"
    [[ -z "${_sub}" && "${_dbg}" == "TRACE" ]] && _sub="ALL"
    if [[ -n "${_sub}" ]]; then
      if grep -q '"NCCL_DEBUG_SUBSYS=' "${_ts}"; then
        _osub="$(grep -m1 -o '"NCCL_DEBUG_SUBSYS=[^"]*"' "${_ts}")"
        sed -i -E "s|\"NCCL_DEBUG_SUBSYS=[^\"]*\"|\"NCCL_DEBUG_SUBSYS=${_sub}\"|g" "${_ts}" \
          && echo "run_llmb_nccl: NCCL_DEBUG_SUBSYS -> ${_sub} (was: ${_osub}) in ${_ts}"
      else
        # Add as a sibling list entry on the line AFTER each "NCCL_DEBUG=..." entry, preserving
        # that line's indentation so it stays inside the same [<gpu>.run_params] list.
        sed -i -E "s|^([[:space:]]*)\"NCCL_DEBUG=[^\"]*\",?$|&\n\\1\"NCCL_DEBUG_SUBSYS=${_sub}\",|" "${_ts}" \
          && echo "run_llmb_nccl: NCCL_DEBUG_SUBSYS -> ${_sub} (entry added) in ${_ts}"
      fi
    fi
   done
  else
    echo "run_llmb_nccl: WARNING: nccl_debug=${_dbg} requested but NO testset.toml with an NCCL_DEBUG key was found" >&2
    echo "run_llmb_nccl: WARNING:   searched LLMB_REPO=${LLMB_REPO:-<unset>} and ${LLMB_INSTALL%/}/workloads/${LLMB_WORKLOAD}" >&2
    echo "run_llmb_nccl: WARNING:   set NVIS_LLMB_TESTSET=<path> — the level was NOT applied" >&2
  fi
else
  echo "run_llmb_nccl: NCCL debug level not applied (nccl_debug=${NCCL_DEBUG:-${NVIS_NCCL_DEBUG:-<unset>}}, apply=${NVIS_LLMB_TESTSET_APPLY:-1})"
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
# CLI changed between llmb releases:
#   v25.10.01 : llmb-run single -w W -s SIZE --dtype D --scale N [--dryrun]
#   v26.06.00+: llmb-run submit -w W -s SIZE --dtype D --scale N [--dry-run]
# `single` was removed ("No such command 'single'"), and --dryrun became --dry-run.
# Probe the installed binary rather than pinning a version.
if "${LLMB_RUN}" submit --help >/dev/null 2>&1; then
  LLMB_SUBCMD="submit"; LLMB_DRY_FLAG="--dry-run"
else
  LLMB_SUBCMD="single"; LLMB_DRY_FLAG="--dryrun"
fi
cmd=( "${LLMB_RUN}" "${LLMB_SUBCMD}" -w "${LLMB_WORKLOAD}" -s "${LLMB_MODEL_SIZE}" --dtype "${LLMB_DTYPE}" --scale "${SCALE}" )
[[ "${DRY_RUN}" -eq 1 ]] && cmd+=( "${LLMB_DRY_FLAG}" )

echo "run_llmb_nccl: workload=${LLMB_WORKLOAD} model_size=${LLMB_MODEL_SIZE} dtype=${LLMB_DTYPE} nodes=${NNODES} scale=${SCALE}"
echo "run_llmb_nccl: nodelist=${NODELIST} (pinned via sbatch --nodelist shim)${RESERVATION:+ reservation=${RESERVATION}}"
echo "run_llmb_nccl: LLMB_INSTALL=${LLMB_INSTALL} LLMB_RUN=${LLMB_RUN}${NVIS_SHIM_DIR:+ sbatch_shim=${NVIS_SHIM_DIR}/sbatch -> ${LLMB_REAL_SBATCH:-}}"
[[ -n "${SBATCH_OUTPUT}" ]] && echo "run_llmb_nccl: sbatch stdout -> ${SBATCH_OUTPUT} (under dashboard log dir)"
echo "run_llmb_nccl: fabric: NCCL_IB_HCA=${NCCL_IB_HCA:-<auto>} NCCL_SOCKET_IFNAME=${NCCL_SOCKET_IFNAME:-<auto>} UCX_TLS=${UCX_TLS:-<default>} UCX_NET_DEVICES=${UCX_NET_DEVICES:-<auto>}"
printf 'run_llmb_nccl: exec:'; printf ' %q' "${cmd[@]}"; echo

cd "${LLMB_INSTALL}" || die "cannot cd to LLMB_INSTALL=${LLMB_INSTALL}"

# llmb-run SUBMITS and returns immediately, so the nccl-tests tables do not exist yet. They land
# under $LLMB_INSTALL/workloads/*/experiments/*_<jobid>/ when the Slurm job finishes, in a layout
# parser.sh cannot see. Capture the jobid, then detach a watcher that waits for the job to leave
# the queue and runs collect_llmb_nccl_log.sh, which writes the aggregated
# <user>-NCCL-1Rack-<rack>-<N>N-<jobid>.txt into the dashboard log dir.
# Disable with NVIS_LLMB_AUTOCOLLECT=0 (then run collect_llmb_nccl_log.sh <jobid> by hand).
_llmb_out="$(mktemp 2>/dev/null || echo /tmp/nvis_llmb_$$.out)"
"${cmd[@]}" 2>&1 | tee "${_llmb_out}"
_rc="${PIPESTATUS[0]}"

if [[ "${DRY_RUN}" -eq 0 ]]; then
  llmb_arm_autocollect \
    "run_llmb_nccl" \
    "$(dirname "${BASH_SOURCE[0]}")/collect_llmb_nccl_log.sh" \
    "${_llmb_out}" \
    "${JOB_LOG_DIR}" \
    "${LLMB_INSTALL}" \
    "llmb-autocollect" || true
fi

rm -f "${_llmb_out}" 2>/dev/null || true
exit "${_rc}"
