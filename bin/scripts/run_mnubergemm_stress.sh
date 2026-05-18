#!/bin/bash
# MNUBERGEMM stress: Slurm job on selected hosts (same hostlist convention as validation / nvloom singlerack).
# Usage: ./run_mnubergemm_stress.sh <pNN-rMM-ct[range]>
# Cwd should be the log directory (Flask sets cwd=log_dir); Slurm stdout is written here.
#
# Workspace: NVIS_WORKSPACE_DIR / RACK_WORKSPACE_DIR / TOPDIR / default (see workspace-dir-common.sh).
# CUDA major for HPC-X tarball name: RACK_MNUBERGEMM_CUDA_MAJOR or RACK_NVLOOM_CUDA_MAJOR or CUDA_MAJ (default 13).
# Optional: RACK_MNUBERGEMM_PARTITION → sbatch -p …
# Optional: NVIS_SLURM_HOSTLIST — comma-separated NodeNames for ``sbatch -w`` (override expansion).

module load slurm

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "${_SCRIPT_DIR}/workspace-dir-common.sh"
# shellcheck disable=SC1091
. "${_SCRIPT_DIR}/slurm-expand-hosts.inc.sh"

export WORKSPACE_DIR
export NVIS_BIN_SCRIPTS="${_SCRIPT_DIR}"
export CUDA_MAJ="${CUDA_MAJ:-${RACK_MNUBERGEMM_CUDA_MAJOR:-${RACK_NVLOOM_CUDA_MAJOR:-13}}}"
: "${NVIS_SYSINFO_SH:=${_SCRIPT_DIR}/sysinfo.sh}"
: "${NVIS_CHECKER_SH:=${_SCRIPT_DIR}/checker.sh}"
export NVIS_SYSINFO_SH
export NVIS_CHECKER_SH

target="${1:?usage: $0 <pNN-rMM-ct[range]>}"
if ! _slurm_expand_to_wlist "${target}"; then
  exit 1
fi

mapfile -t _cancel_ids < <(squeue -h -o '%i %j' 2>/dev/null | awk '$2 ~ /^MNUBERGEMM-/ {print $1}' || true)
if [[ ${#_cancel_ids[@]} -gt 0 ]]; then
  scancel "${_cancel_ids[@]}" 2>/dev/null || true
fi

_nnodes=${_slurm_nnodes}

_sbatch_extra=()
if [[ -n "${RACK_MNUBERGEMM_PARTITION:-}" ]]; then
  _sbatch_extra+=(-p "${RACK_MNUBERGEMM_PARTITION}")
fi

sbatch \
  --export=ALL \
  "${_sbatch_extra[@]}" \
  -N "${_nnodes}" \
  -w "${_slurm_wlist}" \
  -t 2:30:00 \
  --requeue \
  --dependency=singleton \
  --job-name="MNUBERGEMM" \
  --output="${USER}-MNUBERGEMM-${_slurm_wlist%%,*}-%j.txt" \
  "${_SCRIPT_DIR}/mnubergemm_stress.sbatch"
