#!/usr/bin/env bash
# Single-rack ClusterKit Slurm driver: same hostlist convention as run_nvloom_singlerack.sh / run_mnubergemm_stress.sh.
# Slurm stdout: log_dir/clusterkit/${USER}-CK-<rack>-%j.txt ; per-job artifacts under log_dir/clusterkit/<rack>/.
# Cwd must be the rack log directory (Flask sets cwd=log_dir when used from the dashboard).
#
# Usage: ./bin/scripts/run_clusterkit.sh <pNN-rMM-ct[range]>
#
# Workspace: NVIS_WORKSPACE_DIR / RACK_WORKSPACE_DIR / TOPDIR (workspace-dir-common.sh).
# CUDA tarball major: CUDA_MAJ or RACK_NVLOOM_CUDA_MAJOR (default 13).
#
# Optional env:
#   RACK_CLUSTER_PRODUCT_LINE / RACK_PRODUCT_LINE — selects core_to_hca_dgxgb200 vs dgxgb300 (see clusterkit.sbatch).
#   NVIS_CLUSTERKIT_MAPPER — full path to mapper script (overrides product line).
#   RACK_CLUSTERKIT_PARTITION — if set, passed as sbatch -p …
#   RACK_CLUSTERKIT_EXTRA_OPTS — optional extra flags for clusterkit.sh (word-split; see clusterkit.sbatch)
#   NVIS_CLUSTERKIT_HPCX_INIT — override path to hpcx-init.sh

module load slurm

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "${_SCRIPT_DIR}/workspace-dir-common.sh"

export WORKSPACE_DIR
export NVIS_BIN_SCRIPTS="${_SCRIPT_DIR}"
export CUDA_MAJ="${CUDA_MAJ:-${RACK_CLUSTERKIT_CUDA_MAJOR:-${RACK_NVLOOM_CUDA_MAJOR:-13}}}"

target="${1:?usage: $0 <pNN-rMM-ct[range]>}"
rack=$(echo "${target}" | sed -E 's/(p[0-9]+-r[0-9]+)-ct.*/\1/' | tr '[:upper:]' '[:lower:]')

hosts=($(scontrol show hostname "${target}"))
if [[ ${#hosts[@]} -eq 0 ]]; then
  echo "error: scontrol expanded zero hosts for ${target}" >&2
  exit 1
fi

mkdir -p clusterkit

mapfile -t _cancel_ids < <(squeue -h -o '%i %j' 2>/dev/null | awk '$2 ~ /^CLUSTERKIT/ {print $1}' || true)
if [[ ${#_cancel_ids[@]} -gt 0 ]]; then
  scancel "${_cancel_ids[@]}" 2>/dev/null || true
fi

_nnodes=${#hosts[@]}
_wlist=$(IFS=,; echo "${hosts[*]}")

_sbatch_extra=()
if [[ -n "${RACK_CLUSTERKIT_PARTITION:-}" ]]; then
  _sbatch_extra+=(-p "${RACK_CLUSTERKIT_PARTITION}")
fi

export RACK_CLUSTERKIT_RACK="${rack}"

sbatch \
  --export=ALL \
  "${_sbatch_extra[@]}" \
  -N "${_nnodes}" \
  -w "${_wlist}" \
  --requeue \
  --dependency=singleton \
  --job-name="CLUSTERKIT-${rack}" \
  --output="clusterkit/${USER}-CK-${rack}-%j.txt" \
  "${_SCRIPT_DIR}/clusterkit.sbatch"
