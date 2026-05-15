#!/bin/bash
# Single-rack ClusterKit (Slurm): HPC-X clusterkit bandwidth sweep per HCA (validation/clusterkit style).
# Cwd for clusterkit data: log_dir/clusterkit/<rack>/ (Slurm stdout: log_dir/clusterkit/${USER}-CK-<rack>-%j.txt).
# HPC-X is sourced from WORKSPACE_DIR via workspace-dir-common + workspace-cuda-major-common (not hardcoded paths).
#
# Expects: NVIS_BIN_SCRIPTS, RACK_CLUSTERKIT_RACK, SLURM_SUBMIT_DIR=log_dir.
# Optional: RACK_CLUSTER_PRODUCT_LINE / RACK_PRODUCT_LINE (selects core_to_hca mapper),
#            NVIS_CLUSTERKIT_MAPPER (full path override), RACK_CLUSTERKIT_EXTRA_OPTS, CUDA_MAJ / RACK_NVLOOM_CUDA_MAJOR.

#SBATCH --job-name=CLUSTERKIT-rack
#SBATCH --exclusive
#SBATCH --ntasks-per-node=1
#SBATCH --time=04:00:00

module load slurm
cd "${SLURM_SUBMIT_DIR}"

: "${NVIS_BIN_SCRIPTS:?NVIS_BIN_SCRIPTS must point to bin/scripts (set by run_clusterkit.sh)}"
: "${RACK_CLUSTERKIT_RACK:?RACK_CLUSTERKIT_RACK must be set (rack id e.g. p6-r09)}"

# shellcheck disable=SC1091
. "${NVIS_BIN_SCRIPTS}/workspace-dir-common.sh"
: "${CUDA_MAJ:=${RACK_CLUSTERKIT_CUDA_MAJOR:-${RACK_NVLOOM_CUDA_MAJOR:-13}}}"
export CUDA_MAJ

# shellcheck disable=SC1091
. "${NVIS_BIN_SCRIPTS}/workspace-cuda-major-common.sh"

_rack="${RACK_CLUSTERKIT_RACK}"
mkdir -p "clusterkit/${_rack}"
cd "clusterkit/${_rack}"

# HPC-X init under workspace UNPACK (same layout as nvloom / mnubergemm).
if [[ -n "${NVIS_CLUSTERKIT_HPCX_INIT:-}" ]] && [[ -f "${NVIS_CLUSTERKIT_HPCX_INIT}" ]]; then
  _hpcx_init="${NVIS_CLUSTERKIT_HPCX_INIT}"
else
  _hpx="${WORKSPACE_DIR}/${UNPACK}"
  if [[ -f "${_hpx}/hpcx-init.sh" ]]; then
    _hpcx_init="${_hpx}/hpcx-init.sh"
  elif [[ -f "${_hpx}/hpcx-mt-init-ompi.sh" ]]; then
    _hpcx_init="${_hpx}/hpcx-mt-init-ompi.sh"
  else
    echo "ERROR: HPC-X init not found under WORKSPACE_DIR=${WORKSPACE_DIR} (UNPACK=${UNPACK}). Set NVIS_CLUSTERKIT_HPCX_INIT." >&2
    exit 1
  fi
fi

export HPCX_ENABLE_NCCL_MRC_PLUGIN="${HPCX_ENABLE_NCCL_MRC_PLUGIN:-0}"
export HPCX_ENABLE_NCCL_SPCX_PLUGIN="${HPCX_ENABLE_NCCL_SPCX_PLUGIN:-0}"
# shellcheck disable=SC1090
source "${_hpcx_init}"
hpcx_load

if [[ -z "${HPCX_DIR:-}" ]] || [[ ! -d "${HPCX_DIR}/clusterkit" ]]; then
  echo "ERROR: clusterkit not found under HPCX_DIR=${HPCX_DIR:-}(after hpcx_load). Install HPC-X with clusterkit component." >&2
  exit 1
fi

CK_DIR="${HPCX_DIR}/clusterkit"
CK_SH="${CK_DIR}/bin/clusterkit.sh"
if [[ ! -f "${CK_SH}" ]]; then
  echo "ERROR: missing ${CK_SH}" >&2
  exit 1
fi

chmod a+x "${NVIS_BIN_SCRIPTS}"/core_to_hca_dgxgb200.sh "${NVIS_BIN_SCRIPTS}"/core_to_hca_dgxgb300.sh 2>/dev/null || true

if [[ -n "${NVIS_CLUSTERKIT_MAPPER:-}" ]] && [[ -f "${NVIS_CLUSTERKIT_MAPPER}" ]]; then
  CORE_TO_HCA="${NVIS_CLUSTERKIT_MAPPER}"
else
  _pl_raw="${RACK_CLUSTER_PRODUCT_LINE:-${RACK_PRODUCT_LINE:-}}"
  _pl=$(echo "${_pl_raw}" | tr '[:upper:]' '[:lower:]')
  if [[ "${_pl}" == *gb300* ]] || [[ "${_pl}" == *dgxgb300* ]]; then
    CORE_TO_HCA="${NVIS_BIN_SCRIPTS}/core_to_hca_dgxgb300.sh"
  elif [[ "${_pl}" == *gb200* ]] || [[ "${_pl}" == *dgxgb200* ]]; then
    CORE_TO_HCA="${NVIS_BIN_SCRIPTS}/core_to_hca_dgxgb200.sh"
  else
    echo "WARNING: RACK_CLUSTER_PRODUCT_LINE='${_pl_raw}' not recognized; defaulting to core_to_hca_dgxgb200.sh (set NVIS_CLUSTERKIT_MAPPER to override)." >&2
    CORE_TO_HCA="${NVIS_BIN_SCRIPTS}/core_to_hca_dgxgb200.sh"
  fi
fi

HOSTFILE="${PWD}/machinefile_${SLURM_JOB_ID}"
scontrol show hostname "${SLURM_NODELIST}" >"${HOSTFILE}"

hcas=(0 1 2 3)
declare -A hca_name
hca_name["0"]=mlx5_0
hca_name["1"]=mlx5_1
hca_name["2"]=mlx5_2
hca_name["3"]=mlx5_3

mpi_opt="-x CK_OUTPUT_SUBDIR -x CLUSTERKIT_HCA"
DATE=$(date "+%Y%m%d_%H%M%S")

export UCX_TLS="${UCX_TLS:-^dc}"
export UCX_LOG_LEVEL="${UCX_LOG_LEVEL:-info}"

echo "### ClusterKit rack=${_rack} mapper=${CORE_TO_HCA} cwd=${PWD}"
echo "### HPCX_DIR=${HPCX_DIR}"

for i in "${hcas[@]}"; do
  device=${hca_name[${i}]}
  export CK_OUTPUT_SUBDIR="${PWD}/${DATE}_${SLURM_JOB_ID}_${device}"
  export HCA_TAG=${device}
  export CLUSTERKIT_HCA=$i
  set -x
  # Optional: export RACK_CLUSTERKIT_EXTRA_OPTS="--foo ..." (space-separated clusterkit.sh flags).
  # shellcheck disable=SC2086
  "${CK_SH}" --mpi_opt "${mpi_opt}" -r "${HPCX_DIR}" -f "${HOSTFILE}" --mapper "${CORE_TO_HCA}" \
    --exe_opt "-d bw -d lat" --unidirectional ${RACK_CLUSTERKIT_EXTRA_OPTS-}
  rc=$?
  set +x
  if [[ "${rc}" -ne 0 ]]; then
    echo "ERROR: clusterkit.sh failed for HCA index ${i} (${device}), exit ${rc}" >&2
    exit "${rc}"
  fi
done

echo "OK: clusterkit finished on ${SLURM_JOB_NODELIST:-?} (data under ${PWD})"
