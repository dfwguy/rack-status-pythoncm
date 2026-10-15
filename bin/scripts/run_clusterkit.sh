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
# Single-rack ClusterKit Slurm driver: same hostlist convention as run_nvloom_singlerack.sh / run_mnubergemm_stress.sh.
# Slurm stdout: log_dir/clusterkit/${USER}-CK-<rack>-%j.txt ; per-job artifacts under log_dir/clusterkit/<rack>/.
# log_dir is RACK_JOB_LOG_DIR / RACK_STATUS_LOG_DIR, or the launch working directory ($PWD) that
# the app points at the cluster's log_dir.
#
# Usage: ./bin/scripts/run_clusterkit.sh <Slurm hostlist; e.g. pNN-rMM-ct[range] or CBR… prefix[trays]>
#
# Workspace: NVIS_WORKSPACE_DIR / RACK_WORKSPACE_DIR / TOPDIR (workspace-dir-common.sh).
# CUDA tarball major: CUDA_MAJ or RACK_NVLOOM_CUDA_MAJOR (default 13).
#
# Optional env:
#   RACK_CLUSTERKIT_RACK — canonical rack id for logs/paths (dashboard sets this for NVIS/CBR names).
#     If unset: Bright pNN-rMM-ct[…] → pNN-rMM; else trailing […] tray range is stripped from target.
#   NVIS_SLURM_HOSTLIST — comma-separated Slurm NodeNames for ``sbatch -w`` (bypasses scontrol expansion; last resort).
#   NVIS_CLUSTERKIT_MAPPER — full path to a mapper script (default core_to_hca_rail_map.sh, both fabrics).
#   NVIS_FABRIC_NETWORK — the cluster's fabric protocol, IB|RoCE (also auto|mlx|spectrumx|spx); used when RACK_CLUSTERKIT_LINK_TECH is unset (see clusterkit.sbatch).
#   RACK_CLUSTERKIT_LINK_TECH — per-ClusterKit override of the fabric network (see clusterkit.sbatch).
#   RACK_CLUSTERKIT_HCA_PROFILE — detect (default: this node's rails) or a preset (gb300_mlx4, gb300_roce_spx_quad, …; see clusterkit.sbatch).
#   RACK_CLUSTERKIT_UCX_TLS — optional UCX_TLS for RoCE branch (default rc,cuda).
#   RACK_CLUSTERKIT_PARTITION — if set, passed as sbatch -p …
#   RACK_CLUSTERKIT_EXTRA_OPTS — optional extra flags for clusterkit.sh (word-split; see clusterkit.sbatch)
#   NVIS_CLUSTERKIT_HPCX_INIT — override path to hpcx-init.sh
# UCX defaults for Slurm jobs: nvis-ucx-job-env.inc.sh (sourced from clusterkit.sbatch); override UCX_* in the job env.

# Slurm CLI on PATH (non-login SSH shells don't init Modules — see slurm-init.inc.sh).
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/slurm-init.inc.sh"

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOB_LOG_DIR="${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-$PWD}}"
# shellcheck disable=SC1091
. "${_SCRIPT_DIR}/workspace-dir-common.sh"
# shellcheck disable=SC1091
. "${_SCRIPT_DIR}/slurm-expand-hosts.inc.sh"

export WORKSPACE_DIR
export NVIS_BIN_SCRIPTS="${_SCRIPT_DIR}"
export CUDA_MAJ="${CUDA_MAJ:-${RACK_CLUSTERKIT_CUDA_MAJOR:-${RACK_NVLOOM_CUDA_MAJOR:-13}}}"

target="${1:?usage: $0 <pNN-rMM-ct[range]>}"
if [[ -n "${RACK_CLUSTERKIT_RACK:-}" ]]; then
  rack="${RACK_CLUSTERKIT_RACK}"
else
  rack=$(echo "${target}" | sed -E 's/(p[0-9]+-r[0-9]+)-ct.*/\1/' | tr '[:upper:]' '[:lower:]')
  if [[ "$rack" == "$target" ]]; then
    rack=$(echo "${target}" | sed -E 's/\[[^][]*\]$//' | tr '[:upper:]' '[:lower:]')
  fi
fi
export RACK_CLUSTERKIT_RACK="${rack}"

if ! _slurm_expand_to_wlist "${target}"; then
  exit 1
fi

mkdir -p "${JOB_LOG_DIR}/clusterkit" || { echo "run_clusterkit: cannot create log directory: ${JOB_LOG_DIR}/clusterkit" >&2; exit 2; }
cd "${JOB_LOG_DIR}" || exit 2

# Only this target's own stale job: a run for another rack (or across racks) is not ours to cancel.
# (Matching every CLUSTERKIT* job made concurrent rack runs, and a multi-rack run, cancel each other.)
mapfile -t _cancel_ids < <(squeue -h -o '%i %j' 2>/dev/null | awk -v n="CLUSTERKIT-${rack}" '$2 == n {print $1}' || true)
if [[ ${#_cancel_ids[@]} -gt 0 ]]; then
  scancel "${_cancel_ids[@]}" 2>/dev/null || true
fi

_nnodes=${_slurm_nnodes}

# ClusterKit pairs nodes up and refuses an odd count outright ("pairwise tests must be run with an
# even number of nodes") -- every rail then fails with no data. A rack with one node drained or busy
# lands there routinely (U2: 9 of 18 drained, job 192679), so leave the last node out, by name, and
# test the other pairs rather than none.
if (( _nnodes > 2 && _nnodes % 2 == 1 )); then
  _slurm_wlist="${_slurm_wlist%,}"
  _ck_left_out="${_slurm_wlist##*,}"
  _slurm_wlist="${_slurm_wlist%,*}"
  _nnodes=$((_nnodes - 1))
  echo "notice: ClusterKit pairs nodes; ${_nnodes} of $((_nnodes + 1)) schedulable nodes tested, ${_ck_left_out} left out" >&2
fi

if [[ "${_nnodes}" -lt 2 ]]; then
  echo "ERROR: ClusterKit needs at least two Slurm nodes for pairwise NIC latency/bandwidth (expanded count=${_nnodes})." >&2
  echo "       Widen the validation target / tray range, or set NVIS_SLURM_HOSTLIST to two or more NodeNames (comma-separated)." >&2
  echo "       See validation/clusterkit/clusterkit.sh (hostfile must have ≥2 lines)." >&2
  exit 1
fi

_sbatch_extra=()
if [[ -n "${RACK_CLUSTERKIT_PARTITION:-}" ]]; then
  _sbatch_extra+=(-p "${RACK_CLUSTERKIT_PARTITION}")
fi

sbatch \
  --export=ALL \
  "${_sbatch_extra[@]}" \
  -N "${_nnodes}" \
  -w "${_slurm_wlist}" \
  --requeue \
  --dependency=singleton \
  --job-name="CLUSTERKIT-${rack}" \
  --output="${JOB_LOG_DIR%/}/clusterkit/${USER}-CK-${rack}-%j.txt" \
  "${_SCRIPT_DIR}/clusterkit.sbatch"
