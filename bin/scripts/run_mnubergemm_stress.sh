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
# run_mnubergemm_stress.sh — NscaleTX MNUBERGEMM driver (sbatch runner, container-based).
#
# This is the script Rack Status invokes for the MNUBERGEMM button. The endpoint
# /api/run-mnubergemm-stress-async resolves it as:
#     ${RACK_REMOTE_BASE_PATH}/bin/scripts/run_mnubergemm_stress.sh <target>
# i.e. /home/cmods/nvis-rack-status/bin/scripts/run_mnubergemm_stress.sh, executed over
# SSH as cmods@192.168.10.2. Everything below therefore runs ON THE HEAD NODE as cmods.
#
# WHAT CHANGED vs the stock script: the stock one ran the DCGM-plugin `ubergemm` binary out
# of a workspace install (HPC-X + CUDA + NCCL + DCGM staged under $WORKSPACE). That toolchain
# is not installed here, so it failed instantly. Per MOP §7.2 NscaleTX uses the CONTAINER
# instead, and MNUBERGEMM stays on the sbatch runner (llmb-run has no mnubergemm workload —
# see NscaleTX8k_llmbrun/MIGRATION.md "HPL is not ported").
#
# PRE-REQ HANDLING (the point of this script):
#   1. If the container image already exists at $CONT -> use it.
#   2. If it is missing and NGC_API_KEY is set  -> pull + stage it via 06-download-mnubergemm.sh.
#   3. If it is missing and NGC_API_KEY is NOT set -> fail loudly with an explicit message
#      in the log output. No silent skip, no half-run.
#
# Usage:  run_mnubergemm_stress.sh <rack-or-hostlist-target>
#   e.g.  run_mnubergemm_stress.sh ink50-dh01-h04
#
# Environment (Rack Status forwards RACK_* and NGC_API_KEY over SSH):
#   NGC_API_KEY                 NGC key (nvapi-…). Settings -> External connections.
#   RACK_MNUBERGEMM_CONT        override the container path
#   RACK_MNUBERGEMM_TAG         image tag        (default tensorrt-26.03-v1.17-arm)
#   RACK_MNUBERGEMM_IMAGE_DIR   staging dir      (default: the cluster's shared image store, RACK_VALIDATION_CONTAINER_ROOT)
#   RACK_MNUBERGEMM_PARTITION   optional sbatch -p
#   RACK_MNUBERGEMM_TIME        optional shorter walltime (hard maximum 01:00:00)
#   NVIS_SLURM_HOSTLIST         explicit comma-separated NodeName list (skips expansion)
#   RACK_MNUBERGEMM_NO_SUBMIT=1 stage + validate only, do not sbatch (dry run)
set -uo pipefail

_ts()  { date -Is; }
log()  { printf '[%s] %s\n' "$(_ts)" "$*"; }
err()  { printf '[%s] ERROR: %s\n' "$(_ts)" "$*" >&2; }
die()  { err "$*"; exit 1; }

duration_seconds() {
  local days=0 hours=0 minutes=0 seconds=0 rest="$1"
  if [[ "$rest" == *-* ]]; then
    days="${rest%%-*}"; rest="${rest#*-}"
    [[ "$days" =~ ^[0-9]+$ ]] || return 1
  fi
  if [[ "$rest" =~ ^([0-9]+):([0-9]{2}):([0-9]{2})$ ]]; then
    hours="${BASH_REMATCH[1]}"; minutes="${BASH_REMATCH[2]}"; seconds="${BASH_REMATCH[3]}"
  elif [[ "$rest" =~ ^([0-9]+):([0-9]{2})$ ]]; then
    minutes="${BASH_REMATCH[1]}"; seconds="${BASH_REMATCH[2]}"
  elif [[ "$rest" =~ ^[0-9]+$ ]]; then
    minutes="$rest"
  else
    return 1
  fi
  (( minutes < 60 && seconds < 60 )) || return 1
  printf '%d\n' "$(( days * 86400 + hours * 3600 + minutes * 60 + seconds ))"
}

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-$PWD}}"

TAG="${RACK_MNUBERGEMM_TAG:-tensorrt-26.03-v1.17-arm}"
# shellcheck source=lib/node_images.inc.sh
source "${_SCRIPT_DIR}/lib/node_images.inc.sh"
# Where a non-P2P cluster keeps the image for every tray to read: the cluster's shared image store
# (project setting connection.shared_container_dir -> RACK_VALIDATION_CONTAINER_ROOT). On a P2P
# cluster the image is already in each node's cache instead -- see the P2P branch of STEP 2.
IMAGE_DIR="${RACK_MNUBERGEMM_IMAGE_DIR:-${RACK_VALIDATION_CONTAINER_ROOT:-}}"
# MOP/AAT naming: the 'tensorrt-' prefix is dropped in the .sqsh filename, i.e.
#   tag tensorrt-26.03-v1.17-arm  ->  mnubergemm-26.03-v1.17-arm.sqsh
CONT="${RACK_MNUBERGEMM_CONT:-${IMAGE_DIR}/mnubergemm-${TAG#tensorrt-}.sqsh}"
# Rack Status qualifications are capped at one hour. Longer factory burn-ins use a
# separately reviewed workflow; this operator-facing launcher never inherits an unlimited
# partition default. The payload gets five minutes of teardown margin inside that ceiling.
MAX_WALLTIME_SECONDS=3600
MAX_PAYLOAD_SECONDS=3300
TIME_TO_RUN="${RACK_MNUBERGEMM_TIME_TO_RUN:-3300}"
case "${TIME_TO_RUN}" in ''|*[!0-9]*) die "RACK_MNUBERGEMM_TIME_TO_RUN must be seconds (got '${TIME_TO_RUN}')" ;; esac
(( TIME_TO_RUN > 0 && TIME_TO_RUN <= MAX_PAYLOAD_SECONDS )) || \
  die "RACK_MNUBERGEMM_TIME_TO_RUN must be 1-${MAX_PAYLOAD_SECONDS}s (got '${TIME_TO_RUN}')"

# Slurm walltime is always explicit. An optional shorter limit is accepted only when it is
# parseable and no longer than the product's one-hour ceiling.
WALLTIME="${RACK_MNUBERGEMM_TIME:-01:00:00}"
WALLTIME_SECONDS="$(duration_seconds "$WALLTIME")" || \
  die "RACK_MNUBERGEMM_TIME must be a Slurm duration no longer than 01:00:00 (got '${WALLTIME}')"
(( WALLTIME_SECONDS > 0 && WALLTIME_SECONDS <= MAX_WALLTIME_SECONDS )) || \
  die "RACK_MNUBERGEMM_TIME exceeds the 01:00:00 hard maximum (got '${WALLTIME}')"

mkdir -p "${LOG_DIR}" || die "cannot create log directory: ${LOG_DIR}"

log "===================== MNUBERGEMM (sbatch runner) ====================="
log "user=$(id -un) host=$(hostname -s) log_dir=${LOG_DIR}"

# Slurm CLI on PATH: non-login SSH shells do not init Modules, so there is no PATH and no
# SLURM_CONF. slurm-init.inc.sh sets both from absolute paths (globbed, never version-pinned).
if [[ -r "${_SCRIPT_DIR}/slurm-init.inc.sh" ]]; then
  # shellcheck disable=SC1091
  . "${_SCRIPT_DIR}/slurm-init.inc.sh"
else
  _sb="$(ls -d /cm/local/apps/slurm/*/bin 2>/dev/null | sort -V | tail -1)"
  [[ -n "${_sb}" ]] && export PATH="${_sb}:${PATH}"
  _sc="$(ls /cm/shared-*-x86_64/apps/slurm/etc/slurm/slurm.conf 2>/dev/null | head -1)"
  [[ -n "${_sc}" ]] && export SLURM_CONF="${_sc}"
fi
command -v sbatch >/dev/null 2>&1 || die "sbatch not on PATH (slurm init failed)"
log "slurm: $(command -v sbatch) SLURM_CONF=${SLURM_CONF:-<unset>}"

target="${1:-}"
[[ -n "${target}" ]] || die "usage: ${0##*/} <rack-or-hostlist-target>"
log "target: ${target}"

mkdir -p "${LOG_DIR}" 2>/dev/null || true

# ---------------------------------------------------------------------------
# STEP 1 — resolve the node list (drops nodes Slurm cannot schedule)
# ---------------------------------------------------------------------------
if [[ -r "${_SCRIPT_DIR}/slurm-expand-hosts.inc.sh" ]]; then
  # shellcheck disable=SC1091
  . "${_SCRIPT_DIR}/slurm-expand-hosts.inc.sh"
  if ! _slurm_expand_to_wlist "${target}"; then
    die "could not expand '${target}' to any schedulable node"
  fi
elif [[ -n "${NVIS_SLURM_HOSTLIST:-}" ]]; then
  _slurm_wlist="${NVIS_SLURM_HOSTLIST// /}"
  _slurm_nnodes="$(awk -F, '{n=0; for(i=1;i<=NF;i++) if($i!="") n++; print n}' <<<"${_slurm_wlist}")"
else
  die "slurm-expand-hosts.inc.sh missing and NVIS_SLURM_HOSTLIST unset — cannot resolve nodes"
fi
# --- rack health report: show EVERY tray, and name the ones we are dropping + why ---------
# Rack Status passes the FULL expanded hostlist as ${target}, not a rack name. Derive the
# rack prefix from the first host (ink50-dh01-h04-11 -> ink50-dh01-h04); a bare rack name
# passes through unchanged because the trailing element is not all digits.
_first_host="${target%%,*}"
_rackpat="$(printf '%s' "${_first_host}" | sed -E 's/-[0-9]+$//')"
# Short, filesystem-safe tag for log filenames. NEVER use ${target}: 18 hostnames is a
# 323-char string, and the resulting %u-MNUBERGEMM-<target>-%j.txt is 350 bytes — past the
# 255-byte NAME_MAX on ext4/NFS, so Slurm cannot create the stdout file and the job dies
# at launch with no output at all.
_tag="${_rackpat}"

log "--- rack health (sinfo) ---"
if command -v sinfo >/dev/null 2>&1; then
  printf '%-24s %-12s %s\n' "NODE" "STATE" "REASON"
  sinfo -N -h -o '%N|%T|%E' 2>/dev/null | awk -F'|' -v pat="${_rackpat}" '
    index($1, pat) == 1 {
      reason = ($3 == "none" || $3 == "") ? "-" : $3
      printf "%-24s %-12s %s\n", $1, $2, reason
      total++
      if ($2 == "idle") idle++; else { bad++; badlist = badlist " " $1 "(" $2 ": " reason ")" }
    }
    END {
      printf "\n  total=%d  idle=%d  NOT-schedulable=%d\n", total, idle, bad
      if (bad > 0) printf "  EXCLUDED:%s\n", badlist
    }'
else
  log "  (sinfo unavailable — skipping health report)"
fi
log "---------------------------"

NNODES="${_slurm_nnodes}"
WLIST="${_slurm_wlist}"

# Drop operator-excluded nodes. Slurm can report a tray idle while it is unusable for this
# workload — e.g. ink50-dh01-h04-15 refuses SSH for cmods, so it has no /home mount, cannot
# read the container image, and reports no DCGM. Such a node fails its ranks and takes the
# whole job with it. RACK_MNUBERGEMM_EXCLUDE is a comma-separated NodeName list.
if [[ -n "${RACK_MNUBERGEMM_EXCLUDE:-}" ]]; then
  _keep=""; _dropped=""
  IFS=',' read -ra _all <<<"${WLIST}"
  for _n in "${_all[@]}"; do
    [[ -z "${_n}" ]] && continue
    if [[ ",${RACK_MNUBERGEMM_EXCLUDE}," == *",${_n},"* ]]; then
      _dropped="${_dropped:+${_dropped} }${_n}"
    else
      _keep="${_keep:+${_keep},}${_n}"
    fi
  done
  if [[ -n "${_dropped}" ]]; then
    log "EXCLUDED by RACK_MNUBERGEMM_EXCLUDE: ${_dropped}"
    WLIST="${_keep}"
    NNODES="$(awk -F, '{n=0; for(i=1;i<=NF;i++) if($i!="") n++; print n}' <<<"${WLIST}")"
  fi
fi

(( NNODES > 0 )) || die "resolved 0 schedulable nodes for '${target}'"
log "resolved ${NNODES} schedulable node(s): ${WLIST}"
log "GPUs in this run: $(( NNODES * 4 ))  (GB300 = 4 GPU/tray; a full NVL72 domain is 18 trays / 72 GPU)"
if (( NNODES < 18 )); then
  log "NOTE: ${NNODES} < 18 trays — this is NOT a full NVLink-domain test. Down/drained trays were excluded."
fi

# ---------------------------------------------------------------------------
# STEP 2 — container pre-req: present? else pull with the NGC key; else fail loudly
# ---------------------------------------------------------------------------
log "--- container pre-req ---"

if nvis_node_images_p2p && [[ -z "${RACK_MNUBERGEMM_CONT:-}" ]]; then
  # P2P distributes the image into every node's cache; the launch host may not see any image
  # store at all (NC's BCM head mounts only /home). Check each node's copy against the published
  # image catalog -- never pull from the head.
  nvis_stage_node_image run_mnubergemm_stress "${WLIST}" "mnubergemm-${TAG#tensorrt-}.sqsh" \
    || die "image not ready on every node -- nothing was submitted"
  CONT="${NVIS_NEMO_CONT}"
elif [[ -z "${IMAGE_DIR}" && -z "${RACK_MNUBERGEMM_CONT:-}" ]]; then
  die "no image store configured: set the cluster's Shared image store (Projects -> cluster) or RACK_MNUBERGEMM_IMAGE_DIR"
fi
log "expected image: ${CONT}"

if [[ -s "${CONT}" ]] || nvis_node_images_p2p; then
  log "image PRESENT ($(du -h "${CONT}" 2>/dev/null | cut -f1)) — no download needed"
else
  log "image MISSING at ${CONT}"

  if [[ -z "${NGC_API_KEY:-}" ]]; then
    err "======================================================================"
    err "MNUBERGEMM container is not staged AND no NGC API key was supplied."
    err ""
    err "  missing image : ${CONT}"
    err "  required key  : NGC_API_KEY (nvapi-…)"
    err ""
    err "Supply it in Rack Status:  Settings -> External connections -> 'NGC API key'"
    err "(it is forwarded to the head node automatically in SSH exec mode), or export"
    err "NGC_API_KEY before invoking this script."
    err ""
    err "Nothing was submitted. No Slurm job was created."
    err "======================================================================"
    exit 2
  fi

  log "NGC_API_KEY is present (${#NGC_API_KEY} chars) — staging the image now"
  puller="${_SCRIPT_DIR}/06-download-mnubergemm.sh"
  [[ -x "${puller}" || -r "${puller}" ]] || die "puller not found: ${puller}"

  if ! bash "${puller}" "${IMAGE_DIR}" "${TAG}" arm64; then
    die "container pull failed — see the log above. Nothing was submitted."
  fi

  # The puller writes enroot's canonical name (org+team+image+tag). Accept whatever
  # landed for this tag rather than assuming one spelling.
  if [[ ! -s "${CONT}" ]]; then
    found=""
    for f in "${IMAGE_DIR}"/*mnubergemm*"${TAG}"*.sqsh; do
      [[ -s "${f}" ]] && { found="${f}"; break; }
    done
    [[ -n "${found}" ]] || die "pull reported success but no .sqsh matching '*mnubergemm*${TAG}*' is in ${IMAGE_DIR}"
    CONT="${found}"
    log "staged image resolved to: ${CONT}"
  fi
  log "image STAGED ($(du -h "${CONT}" 2>/dev/null | cut -f1))"
fi

# The trays mount this over NFS — a local-only path would fail on every rank but one. (A P2P
# node-cache path is local by design and was verified on every node above.)
nvis_node_images_p2p || case "${CONT}" in
  /cm/shared/*|/home/*) : ;;
  *) log "WARNING: ${CONT} may not be on a shared filesystem; every tray must be able to read it." ;;
esac

# ---------------------------------------------------------------------------
# STEP 3 — cancel any previous MNUBERGEMM job, then submit
# ---------------------------------------------------------------------------
# Scope the cancel to THIS RACK's jobs only.
# BUG FIXED 2026-07-28: the previous version matched `^MNUBERGEMM` against a flat job-name shared
# by every rack, so it cancelled MNUBERGEMM jobs CLUSTER-WIDE. Firing h03 killed h01's job 63870
# 11 minutes into a 30-minute soak (CANCELLED, ExitCode 0:15/SIGTERM). Multi-rack soaks are a
# normal thing to want; one rack's run must never terminate another's.
_JOB_NAME="MNUBERGEMM-${_tag}"
SBATCH_FILE="${_SCRIPT_DIR}/mnubergemm.sbatch"
[[ -r "${SBATCH_FILE}" ]] || die "sbatch file not found: ${SBATCH_FILE}"

extra=(-t "${WALLTIME}")
[[ -n "${RACK_MNUBERGEMM_PARTITION:-}" ]] && extra+=(-p "${RACK_MNUBERGEMM_PARTITION}")

# A staging-only validation must never mutate the queue. Keep this before the
# same-rack cancel block so RACK_MNUBERGEMM_NO_SUBMIT=1 is genuinely read-only.
if [[ "${RACK_MNUBERGEMM_NO_SUBMIT:-0}" == "1" ]]; then
  log "RACK_MNUBERGEMM_NO_SUBMIT=1 — staging validated, not submitting."
  log "Would run: sbatch ${extra[*]} -N ${NNODES} -w ${WLIST} ${SBATCH_FILE}"
  exit 0
fi

mapfile -t _old < <(squeue -h -o '%i %j' 2>/dev/null | awk -v n="${_JOB_NAME}" '$2 == n {print $1}' || true)
if (( ${#_old[@]} > 0 )); then
  log "cancelling ${#_old[@]} previous ${_JOB_NAME} job(s) on this rack: ${_old[*]}"
  scancel "${_old[@]}" 2>/dev/null || true
else
  log "no previous ${_JOB_NAME} job to cancel (other racks' runs are left alone)"
fi

# MOP §7.2 is just `sbatch -N <n> mnubergemm.sbatch`. The only flags added here are the ones
# Rack Status structurally needs: -w (target a specific rack), --output (land logs in the
# configured log dir), --job-name (so the cancel-previous step above can find them).
# Deliberately NOT added: --dependency=singleton (the scancel above already prevents overlap).
# --no-requeue IS added: this site runs JobRequeue=1, so a tray dying mid-soak would silently
# requeue and re-run — destroying the evidence for the MOP success criterion "No unexpected
# node failures or reboots". A stress test must fail visibly, not restart.
export NVIS_MNUBERGEMM_CONT="${CONT}"
export MNUBERGEMM_TIME_TO_RUN="${TIME_TO_RUN}"

log "--- submitting ---"
log "sbatch ${extra[*]} -N ${NNODES} -w ${WLIST} ${SBATCH_FILE}"
log "soak length: ${TIME_TO_RUN}s ($(( TIME_TO_RUN/60 ))m) — Rack Status qualification (hard maximum 60m)."
log "walltime: ${WALLTIME} (explicit; never inherits an unlimited partition default)"

cd "${LOG_DIR}" 2>/dev/null || true

out="$(sbatch \
  --export=ALL \
  "${extra[@]}" \
  -N "${NNODES}" \
  -w "${WLIST}" \
  --no-requeue \
  --job-name="${_JOB_NAME}" \
  --output="${LOG_DIR}/%u-MNUBERGEMM-${_tag}-%j.txt" \
  "${SBATCH_FILE}" 2>&1)"
rc=$?
log "${out}"
(( rc == 0 )) || die "sbatch failed (rc=${rc})"

jobid="$(grep -oE '[0-9]+' <<<"${out}" | tail -1)"
log "submitted job ${jobid:-<unknown>}"
log "log file: ${LOG_DIR}/${USER}-MNUBERGEMM-${_tag}-${jobid}.txt"
log "watch:    squeue -j ${jobid}   |   tail -f ${LOG_DIR}/*MNUBERGEMM-${_tag}-${jobid}.txt"
log "======================================================================"
exit 0
