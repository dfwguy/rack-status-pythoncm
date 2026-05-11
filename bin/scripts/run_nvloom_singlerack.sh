#!/usr/bin/env bash
# nvloom single-rack Slurm job: expands a Slurm hostlist (same convention as validation / run_mnubergemm_stress.sh),
# writes Slurm stdout under log_dir/nvloom/. Cwd must be the log directory (Flask sets cwd=log_dir).
#
# Usage: ./run_nvloom_singlerack.sh <pNN-rMM-ct[range]>
#
# Workspace (HPC-X unpack + nvloom build): set NVIS_WORKSPACE_DIR or RACK_WORKSPACE_DIR or TOPDIR, or rely on
# workspace-dir-common.sh default. CUDA major for HPC-X tarball name: CUDA_MAJ (default 13).
#
# Optional env:
#   RACK_NVLOOM_PARTITION — if set, passed as sbatch -p …
#   RACK_SAMPLES — forwarded to the batch script (default 5)
#   NVIS_SYSINFO_SH — forwarded via sbatch --export=ALL (Flask sets from RACK_SYSINFO_SCRIPT)
#   NVIS_NVLOOM_HPCX_INIT, NVIS_NVLOOM_CLI — optional overrides for non-workspace paths

module load slurm

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "${_SCRIPT_DIR}/workspace-dir-common.sh"

export WORKSPACE_DIR
export NVIS_BIN_SCRIPTS="${_SCRIPT_DIR}"
export CUDA_MAJ="${CUDA_MAJ:-${NVIS_NVLOOM_CUDA_MAJOR:-13}}"

target="${1:?usage: $0 <pNN-rMM-ct[range]>}"
# Same rack token as run_singlerack.sh (pNN-rMM from pNN-rMM-ct[…]).
rack=$(echo "${target}" | sed -E 's/(p[0-9]+-r[0-9]+)-ct.*/\1/' | tr '[:upper:]' '[:lower:]')

hosts=($(scontrol show hostname "${target}"))
if [[ ${#hosts[@]} -eq 0 ]]; then
  echo "error: scontrol expanded zero hosts for ${target}" >&2
  exit 1
fi

mkdir -p nvloom

mapfile -t _cancel_ids < <(squeue -h -o '%i %j' 2>/dev/null | awk '$2 ~ /^nvloom/ {print $1}' || true)
if [[ ${#_cancel_ids[@]} -gt 0 ]]; then
  scancel "${_cancel_ids[@]}" 2>/dev/null || true
fi

_nnodes=${#hosts[@]}
_wlist=$(IFS=,; echo "${hosts[*]}")

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
  -w "${_wlist}" \
  --requeue \
  --dependency=singleton \
  --output="nvloom/${USER}-NVLOOM-${rack}-%j.txt" \
  "${_SCRIPT_DIR}/nvloom_singlerack.sbatch"
