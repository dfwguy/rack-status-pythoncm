#!/bin/bash
# MNUBERGEMM stress: per-node sbatch --wait (same Slurm target convention as validation / run_singlerack).
# Usage: ./run_mnubergemm_stress.sh <pNN-rMM-ct[range]>
# Cwd should be the log directory (Flask sets cwd=log_dir); Slurm stdout goes here.

module load slurm

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# scripts -> bin -> nvis-rack-status -> parent workspace (e.g. /home/cmsupport)
_CMSUP="$(cd "$_SCRIPT_DIR/../../.." && pwd)"

target="${1:?usage: $0 <pNN-rMM-ct[range]>}"
hosts=($(scontrol show hostname "${target}"))
if [[ ${#hosts[@]} -eq 0 ]]; then
  echo "error: scontrol expanded zero hosts for ${target}" >&2
  exit 1
fi

mapfile -t _cancel_ids < <(squeue -h -o '%i %j' 2>/dev/null | awk '$2 ~ /^MNUBERGEMM-/ {print $1}' || true)
if [[ ${#_cancel_ids[@]} -gt 0 ]]; then
  scancel "${_cancel_ids[@]}" 2>/dev/null || true
fi



sbatch \
  -N 18 \
  -w $(echo "${hosts[*]}"|tr ' ' ',') \
  -t 2:30:00 \
  --requeue \
  --dependency=singleton \
  --job-name="MNUBERGEMM" \
  --output="${USER}-MNUBERGEMM-${h}-%j.txt" \
  "${_SCRIPT_DIR}/mnubergemm_stress.sbatch"
 