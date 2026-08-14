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
# nvloom single-rack Slurm job: expands a Slurm hostlist (same convention as validation / run_mnubergemm_stress.sh),
# writes Slurm stdout under log_dir/nvloom/. Manual runs use the shared cluster log root.
#
# Usage: ./run_nvloom_singlerack.sh <pNN-rMM-ct[range]>
#
# Workspace (HPC-X unpack + nvloom build): set NVIS_WORKSPACE_DIR or RACK_WORKSPACE_DIR or TOPDIR, or rely on
# workspace-dir-common.sh default. CUDA major for HPC-X tarball name: CUDA_MAJ (default 13).
#
# Optional env:
#   RACK_NVLOOM_SINGLERACK_RACK — canonical rack id for log filenames; dashboard sets for NVIS/CBR.
#     If unset: Bright pNN-rMM-ct[…] → pNN-rMM; else strip trailing […] from target.
#   NVIS_SLURM_HOSTLIST — comma-separated NodeNames for ``sbatch -w`` (override expansion).
#   RACK_NVLOOM_PARTITION — if set, passed as sbatch -p …
#   NVIS_SYSINFO_SH — forwarded via sbatch --export=ALL (Flask sets from RACK_SYSINFO_SCRIPT)
#   NVIS_NVLOOM_HPCX_INIT, NVIS_NVLOOM_CLI — optional overrides for non-workspace paths
#   NVIS_NVLOOM_UCX_TLS — optional UCX_TLS for the nvloom srun step (see nvloom_singlerack.sbatch)

# Slurm CLI on PATH (non-login SSH shells don't init Modules — see slurm-init.inc.sh).
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/slurm-init.inc.sh"

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOB_LOG_DIR="${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-/cluster/benchmark/harish/rack_status/logs}}"
# shellcheck disable=SC1091
. "${_SCRIPT_DIR}/workspace-dir-common.sh"
# shellcheck disable=SC1091
. "${_SCRIPT_DIR}/slurm-expand-hosts.inc.sh"

export WORKSPACE_DIR
export NVIS_BIN_SCRIPTS="${_SCRIPT_DIR}"
export CUDA_MAJ="${CUDA_MAJ:-${NVIS_NVLOOM_CUDA_MAJOR:-13}}"

target="${1:?usage: $0 <pNN-rMM-ct[range]>}"
if [[ -n "${RACK_NVLOOM_SINGLERACK_RACK:-}" ]]; then
  rack="${RACK_NVLOOM_SINGLERACK_RACK}"
else
  rack=$(echo "${target}" | sed -E 's/(p[0-9]+-r[0-9]+)-ct.*/\1/' | tr '[:upper:]' '[:lower:]')
  if [[ "$rack" == "$target" ]]; then
    rack=$(echo "${target}" | sed -E 's/\[[^][]*\]$//' | tr '[:upper:]' '[:lower:]')
  fi
fi

if ! _slurm_expand_to_wlist "${target}"; then
  exit 1
fi

mkdir -p "${JOB_LOG_DIR}/nvloom" || { echo "run_nvloom_singlerack: cannot create log directory: ${JOB_LOG_DIR}/nvloom" >&2; exit 2; }
cd "${JOB_LOG_DIR}" || exit 2

mapfile -t _cancel_ids < <(squeue -h -o '%i %j' 2>/dev/null | awk '$2 ~ /^nvloom/ {print $1}' || true)
if [[ ${#_cancel_ids[@]} -gt 0 ]]; then
  scancel "${_cancel_ids[@]}" 2>/dev/null || true
fi

_nnodes=${_slurm_nnodes}

_sbatch_extra=()
if [[ -n "${RACK_NVLOOM_PARTITION:-}" ]]; then
  _sbatch_extra+=(-p "${RACK_NVLOOM_PARTITION}")
fi

# --wait: keep the dashboard async job in "running" until Slurm completes.
# Slurm stdout: under nvloom/, same style as NEMO (${USER}-NEMO-1Rack-${rack}-…).
sbatch \
  --export=ALL \
  "${_sbatch_extra[@]}" \
  -N "${_nnodes}" \
  -w "${_slurm_wlist}" \
  --requeue \
  --dependency=singleton \
  --output="${JOB_LOG_DIR%/}/nvloom/${USER}-NVLOOM-${rack}-%j.txt" \
  "${_SCRIPT_DIR}/nvloom_singlerack.sbatch"
