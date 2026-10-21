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
# run_llmb_nemo.sh — fire the NEMO (nemotron-h) pretrain validation through
# `llmb-run`, so NEMO and NCCL come from the SAME harness.
#
# Why this exists: the dashboard's validation action ran `run_singlerack.sh`, which
# hand-rolls its own sbatch + pyxis invocation. NCCL moved to llmb-run on 2026-07-28;
# NEMO did not, so the two halves of the campaign were produced by different harnesses
# (different container invocation, env, and parallelism defaults) and were not strictly
# comparable. This closes that gap.
#
# DROP-IN for run_singlerack.sh: the app calls
#     bash <script> <comma-separated-node-list> <rack-id>
# (app.py `cmd = ["bash", str(script), target, rk]`), so this script accepts exactly
# that, and is selected purely by pointing RACK_VALIDATION_SCRIPT at it — no backend
# change. Flag form is also accepted for CLI testing.
#
# Shared machinery (identical to run_llmb_nccl.sh — deliberately, so both paths get
# the same fabric wireup and log placement):
#   lib/nodes.inc.sh       rack ids / nodelist -> Slurm nodelist + GPU scale
#   lib/fabric.inc.sh      NCCL/UCX/OMPI onto the compute rails
#   lib/sbatch_pin.inc.sh  pin llmb's own sbatch to the nodelist + force --output
#
# Usage:
#   run_llmb_nemo.sh <nodelist-csv> <rack-id>            # app contract
#   run_llmb_nemo.sh [--dry-run] --nodelist LIST [--rack R] [--scale N]
#                    [--dtype fp8] [--model-size 56b] [--time-limit 01:00:00] [--profile]
#                    [--mbridge-arg ARG]...
#
# Environment:
#   NVIS_LLMB_NEMO_TIME_LIMIT  walltime handed to the recipe (default 01:00:00).
#                              nemotron-h/launch.sh defaults to 00:20:00, which is too
#                              short for a meaningful run; passed via llmb-run --env.
#   NVIS_LLMB_NEMO_WORKLOAD    default pretrain_nemotron-h
#   NVIS_LLMB_NEMO_MODEL_SIZE  default 56b
#   NVIS_LLMB_NEMO_DTYPE       default fp8
#   NVIS_LLMB_NEMO_PROFILE=1   same as --profile: llmb-run -p, the recipe's Nsight Systems capture
#                              (ENABLE_PROFILE; its own step window). A suite run keeps it off.
#   NVIS_LLMB_NEMO_MBRIDGE_ARGS  whitespace-separated Megatron-Bridge arguments, same as repeating
#                              --mbridge-arg: each goes to llmb-run --mbridge-arg, which appends it after
#                              the recipe's own overrides (MBridge --flag=value or a Hydra key=value).
#                              llmb-run rejects whitespace inside one argument.
#   NVIS_FABRIC_SKIP=1         skip the fabric probe
#   NVIS_LLMB_KEEP_UCX_ENV=1   keep host UCX pins (host/non-container runs only)
set -uo pipefail

GPUS_PER_NODE="${GPUS_PER_NODE:-4}"
# LLMB_INSTALL / LLMB_VENV default under the cluster's workspace (project setting
# connection.workspace_dir -> NVIS_WORKSPACE_DIR), exactly where install-dgxc-benchmarking-workspace.sh
# puts them; an explicit LLMB_INSTALL / LLMB_VENV still wins. lib/llmb_paths.inc.sh fails with the
# setting's name when it needs a workspace and none is configured.
# shellcheck source=lib/llmb_paths.inc.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/llmb_paths.inc.sh" || exit 2
LLMB_REPO="${LLMB_REPO:-}"
LLMB_REPO="${LLMB_REPO%/}"
LLMB_RUN="${LLMB_RUN:-}"
JOB_LOG_DIR="${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-$PWD}}"

LLMB_WORKLOAD="${NVIS_LLMB_NEMO_WORKLOAD:-pretrain_nemotron-h}"
LLMB_MODEL_SIZE="${NVIS_LLMB_NEMO_MODEL_SIZE:-56b}"
LLMB_DTYPE="${NVIS_LLMB_NEMO_DTYPE:-fp8}"
TIME_LIMIT_VAL="${NVIS_LLMB_NEMO_TIME_LIMIT:-01:00:00}"
PROFILE=0
MBRIDGE_ARGS=()
read -r -a MBRIDGE_ARGS <<< "${NVIS_LLMB_NEMO_MBRIDGE_ARGS:-}"
[[ "${NVIS_LLMB_NEMO_PROFILE:-0}" == "1" ]] && PROFILE=1

DRY_RUN=0
EXPLICIT_NODELIST=""
SCALE_OVERRIDE=""
RESERVATION="${NVIS_RESERVATION:-}"
RACK_ID=""
POSITIONAL=()

die() { echo "run_llmb_nemo: $*" >&2; exit 2; }

mkdir -p "${JOB_LOG_DIR}" || die "cannot create log directory: ${JOB_LOG_DIR}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -d|--dry-run|--dryrun) DRY_RUN=1; shift ;;
    --profile) PROFILE=1; shift ;;
    --mbridge-arg) MBRIDGE_ARGS+=( "${2:?--mbridge-arg needs a value}" ); shift 2 ;;
    --mbridge-arg=*) MBRIDGE_ARGS+=( "${1#*=}" ); shift ;;
    --nodelist) EXPLICIT_NODELIST="${2:?--nodelist needs a value}"; shift 2 ;;
    --nodelist=*) EXPLICIT_NODELIST="${1#*=}"; shift ;;
    --rack) RACK_ID="${2:?--rack needs a value}"; shift 2 ;;
    --rack=*) RACK_ID="${1#*=}"; shift ;;
    --scale) SCALE_OVERRIDE="${2:?--scale needs a value}"; shift 2 ;;
    --scale=*) SCALE_OVERRIDE="${1#*=}"; shift ;;
    --dtype) LLMB_DTYPE="${2:?}"; shift 2 ;;
    --dtype=*) LLMB_DTYPE="${1#*=}"; shift ;;
    --model-size) LLMB_MODEL_SIZE="${2:?}"; shift 2 ;;
    --model-size=*) LLMB_MODEL_SIZE="${1#*=}"; shift ;;
    --time-limit) TIME_LIMIT_VAL="${2:?}"; shift 2 ;;
    --time-limit=*) TIME_LIMIT_VAL="${1#*=}"; shift ;;
    --reservation) RESERVATION="${2:?}"; shift 2 ;;
    --reservation=*) RESERVATION="${1#*=}"; shift ;;
    -h|--help) sed -n '1,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
    --) shift; while [[ $# -gt 0 ]]; do POSITIONAL+=( "$1" ); shift; done ;;
    -*) die "unknown option: $1" ;;
    *) POSITIONAL+=( "$1" ); shift ;;
  esac
done

# run_singlerack.sh contract: $1 = target node list (csv), $2 = rack id.
if [[ -z "${EXPLICIT_NODELIST}" && ${#POSITIONAL[@]} -ge 1 ]]; then
  EXPLICIT_NODELIST="${POSITIONAL[0]}"
fi
if [[ -z "${RACK_ID}" && ${#POSITIONAL[@]} -ge 2 ]]; then
  RACK_ID="${POSITIONAL[1]}"
fi
[[ -n "${EXPLICIT_NODELIST}" ]] || die "no target: pass a node list as \$1 or --nodelist LIST"

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
nvis_nodes_resolve --nodelist "${EXPLICIT_NODELIST}" || exit $?
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
  die "llmb-run not found (searched under LLMB_INSTALL=${LLMB_INSTALL}${LLMB_REPO:+ and LLMB_REPO=${LLMB_REPO}})."
fi
# shellcheck disable=SC1091
[[ -f "${LLMB_VENV}/bin/activate" ]] && source "${LLMB_VENV}/bin/activate"
export LLMB_INSTALL

# What this launch states (the cluster's nccl_env for its span, the dialog's variables) is captured
# before the fabric wiring below and restored after it, so a stated value wins over every pick the
# probe and the container wiring make (lib/stated_env.inc.sh).
# shellcheck source=lib/stated_env.inc.sh
source "${_SCRIPT_DIR}/lib/stated_env.inc.sh"
nvis_stated_env_capture

# --- Fabric wireup --------------------------------------------------------------
if [[ "${NVIS_FABRIC_SKIP:-0}" != "1" ]]; then
  _first_node="$(nvis_nodes_first)"
  if [[ -n "${_first_node}" ]]; then
    nvis_fabric_export_from_node "${_first_node}" || true
  else
    echo "run_llmb_nemo: WARNING: could not determine a representative node for fabric probe" >&2
  fi
fi

# --- Container reality check + wiring for this fabric ----------------------------
# Same machinery as run_llmb_nccl.sh (lib/llmb_env.inc.sh): drop the probe's host-only UCX pins
# (UCX_TLS=^tcp killed job 65827 on every tray), give the container the RDMA devices and the cluster's
# NCCL net plugin, set MPI's own transport for the fabric, keep what this launch stated, and force it
# all into the job's --export. Before this the NeMo half of the action ran its ranks with no RDMA device
# in the container, so a multi-rack pretrain fell back to TCP sockets, as the NCCL sweep did (Slurm
# 189003) before the wiring was given to it.
# shellcheck source=lib/llmb_env.inc.sh
source "${_SCRIPT_DIR}/lib/llmb_env.inc.sh"
nvis_llmb_container_wiring run_llmb_nemo

# --- Stage 1 of this action: the NCCL sweep, also via llmb-run --------------------
# MUST run BEFORE nvis_sbatch_pin_install below. The shim works by prepending a fake
# `sbatch` to PATH and recording the real one in LLMB_REAL_SBATCH. run_llmb_nccl.sh
# installs its OWN shim, and if ours is already on PATH its `command -v sbatch` resolves
# to OUR shim -- so the inner shim execs the outer shim, which now points at itself.
# Result: sbatch is never reached and the action hangs forever with no job submitted
# (observed on h22: two shim processes alive 8+ minutes, rack idle, squeue empty).
#
# Why llmb-run at all for NCCL: the old sbatch validation ran an NCCL pre-check INSIDE
# the NEMO job, so one action produced both halves. llmb-run has no such bundle -- NCCL
# is its own workload -- so submitting only the pretrain silently dropped half the test.
# Both jobs request all 18 trays, so Slurm serialises them on the rack by itself.
# Set NVIS_LLMB_NEMO_WITH_NCCL=0 for a NEMO-only submit.
if [[ "${NVIS_LLMB_NEMO_WITH_NCCL:-1}" == "1" && "${DRY_RUN}" -eq 0 ]]; then
  _nccl_script="${_SCRIPT_DIR}/run_llmb_nccl.sh"
  if [[ -f "${_nccl_script}" ]]; then
    echo "run_llmb_nemo: === stage 1/2: NCCL sweep via run_llmb_nccl.sh ==="
    # Pass the RACK ID, not --nodelist: run_llmb_nccl.sh derives its log label from
    # RACKS[], and --nodelist leaves that empty, producing llmb-nccl_nodes_<jid>.out with
    # no rack attribution. Resolving a rack id needs these two vars at this site.
    _nccl_rc=0
    if [[ -n "${RACK_ID}" ]]; then
      RACK_TO_NODE_SED="${RACK_TO_NODE_SED:-s/\$/-/}" \
      RACK_NODE_MATCH_SUFFIX="${RACK_NODE_MATCH_SUFFIX:-[0-9]+\$}" \
      RACK_JOB_LOG_DIR="${JOB_LOG_DIR}" \
        bash "${_nccl_script}" "${RACK_ID}" || _nccl_rc=$?
    else
      RACK_JOB_LOG_DIR="${JOB_LOG_DIR}" bash "${_nccl_script}" --nodelist "${NODELIST}" || _nccl_rc=$?
    fi
    # Do NOT swallow this. A silent stage-1 failure is how h03 got a NEMO-only run
    # while the action claimed to do NCCL+NEMO.
    if [[ "${_nccl_rc}" -ne 0 ]]; then
      echo "run_llmb_nemo: ERROR: NCCL stage failed (rc=${_nccl_rc}); NEMO will still be submitted," \
           "but this rack has NO stage-1 result from this action." >&2
    fi
    echo "run_llmb_nemo: === stage 2/2: NEMO pretrain ==="
  else
    echo "run_llmb_nemo: WARNING: ${_nccl_script} not found; submitting NEMO only" >&2
  fi
fi

# --- Rank layout: racks together, rank 0's host named ------------------------------
# After the NCCL stage, which is its own launch, and before llmb submits: the job's srun reads
# SLURM_HOSTFILE + SLURM_DISTRIBUTION=arbitrary, so tensor/expert groups of consecutive ranks stay
# inside one NVLink domain, and MASTER_ADDR names the host rank 0 runs on (lib/rank_layout.inc.sh).
# shellcheck source=lib/rank_layout.inc.sh
source "${_SCRIPT_DIR}/lib/rank_layout.inc.sh"
nvis_rank_layout run_llmb_nemo "${NODELIST}" "${GPUS_PER_NODE}" "${JOB_LOG_DIR:-.}" \
  || echo "run_llmb_nemo: WARNING: no rank layout; ranks follow Slurm's node order" >&2

# --- Pin llmb's own sbatch + name the log the way the dashboard expects ---------
# parser.sh / rack_status.sh key on  <user>-NEMO-1Rack-<rack>-<N>N-<jobid>.txt.
# The shim STRIPS llmb's own --output and forces ours, and Slurm expands %j, so the
# aggregated log lands under the dashboard log dir already correctly named — no
# post-run collector needed (unlike NCCL, whose tables live in per-rank files).
_rack_label="${RACK_ID:-nodes}"
_rack_label="$(printf '%s' "${_rack_label}" | tr -c 'A-Za-z0-9._+-' '_')"
SBATCH_OUTPUT=""
if [[ -n "${JOB_LOG_DIR}" && -d "${JOB_LOG_DIR}" ]]; then
  SBATCH_OUTPUT="${JOB_LOG_DIR%/}/${USER:-cmods}-NEMO-1Rack-${_rack_label}-${NNODES}N-%j.txt"
fi

trap nvis_sbatch_pin_cleanup EXIT
if [[ "${DRY_RUN}" -eq 0 ]]; then
  nvis_sbatch_pin_install "${NODELIST}" "${SBATCH_OUTPUT}"
fi
[[ -n "${RESERVATION}" ]] && export SBATCH_RESERVATION="${RESERVATION}"

# --- Build + run llmb-run -------------------------------------------------------
if "${LLMB_RUN}" submit --help >/dev/null 2>&1; then
  LLMB_SUBCMD="submit"; LLMB_DRY_FLAG="--dry-run"
else
  LLMB_SUBCMD="single"; LLMB_DRY_FLAG="--dryrun"
fi
cmd=( "${LLMB_RUN}" "${LLMB_SUBCMD}" -w "${LLMB_WORKLOAD}" -s "${LLMB_MODEL_SIZE}"
      --dtype "${LLMB_DTYPE}" --scale "${SCALE}" )
# TIME_LIMIT is read by nemotron-h/launch.sh (default 00:20:00) and passed to its
# --time_limit. --env is the supported override channel in llmb-run v26.06+.
[[ -n "${TIME_LIMIT_VAL}" ]] && cmd+=( --env "TIME_LIMIT=${TIME_LIMIT_VAL}" )
[[ "${PROFILE}" -eq 1 ]] && cmd+=( -p )
for _mb in "${MBRIDGE_ARGS[@]}"; do cmd+=( "--mbridge-arg=${_mb}" ); done
[[ "${DRY_RUN}" -eq 1 ]] && cmd+=( "${LLMB_DRY_FLAG}" )

echo "run_llmb_nemo: workload=${LLMB_WORKLOAD} model_size=${LLMB_MODEL_SIZE} dtype=${LLMB_DTYPE} nodes=${NNODES} scale=${SCALE}"
echo "run_llmb_nemo: rack=${RACK_ID:-<none>} nodelist=${NODELIST} time_limit=${TIME_LIMIT_VAL}"
[[ ${#MBRIDGE_ARGS[@]} -gt 0 ]] && echo "run_llmb_nemo: mbridge args: ${MBRIDGE_ARGS[*]}"
echo "run_llmb_nemo: LLMB_INSTALL=${LLMB_INSTALL} LLMB_RUN=${LLMB_RUN}${NVIS_SHIM_DIR:+ sbatch_shim=${NVIS_SHIM_DIR}/sbatch}"
[[ -n "${SBATCH_OUTPUT}" ]] && echo "run_llmb_nemo: sbatch stdout -> ${SBATCH_OUTPUT}"
echo "run_llmb_nemo: fabric: NCCL_IB_HCA=${NCCL_IB_HCA:-<auto>} NCCL_SOCKET_IFNAME=${NCCL_SOCKET_IFNAME:-<auto>} UCX_TLS=${UCX_TLS:-<default>} UCX_NET_DEVICES=${UCX_NET_DEVICES:-<auto>}"
echo "run_llmb_nemo: container: MELLANOX_VISIBLE_DEVICES=${MELLANOX_VISIBLE_DEVICES:-<unset>} NCCL_NET_PLUGIN=${NCCL_NET_PLUGIN:-<NCCL default>} forced into job env: ${NVIS_FORCE_EXPORT_VARS:-<none>}"
printf 'run_llmb_nemo: exec:'; printf ' %q' "${cmd[@]}"; echo

cd "${LLMB_INSTALL}" || die "cannot cd to LLMB_INSTALL=${LLMB_INSTALL}"

_out="$(mktemp 2>/dev/null || echo /tmp/nvis_llmb_nemo_$$.out)"
"${cmd[@]}" 2>&1 | tee "${_out}"
_rc="${PIPESTATUS[0]}"

# --- Auto-collect: llmb's REAL training output is not in our sbatch stdout --------
# nemo_run's generated batch script runs the work under its own
# `srun --output <experiments>/.../log-default-default...out`, which our sbatch shim
# never sees. Without this the canonical log holds ~6 KB of shell trace and zero
# iterations, and the dashboard reports a successful run as "not run" (jobs 84733-84736).
# Disable with NVIS_LLMB_AUTOCOLLECT=0.
if [[ "${DRY_RUN}" -eq 0 ]]; then
  # The rack id is an extra collector arg: the NeMo collector needs it to name the aggregated log.
  llmb_arm_autocollect \
    "run_llmb_nemo" \
    "${_SCRIPT_DIR}/collect_llmb_nemo_log.sh" \
    "${_out}" \
    "${JOB_LOG_DIR}" \
    "${LLMB_INSTALL}" \
    "llmb-nemo-autocollect" \
    "${RACK_ID}" || true
fi

rm -f "${_out}" 2>/dev/null || true
echo "run_llmb_nemo: llmb-run exit=${_rc}"
exit "${_rc}"
