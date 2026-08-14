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
# Single-rack ClusterKit (Slurm): HPC-X clusterkit bandwidth sweep per HCA (validation/clusterkit style).
# Cwd for clusterkit data: log_dir/clusterkit/<rack>/ (Slurm stdout: log_dir/clusterkit/${USER}-CK-<rack>-%j.txt).
# HPC-X is sourced from WORKSPACE_DIR via workspace-dir-common + workspace-cuda-major-common (not hardcoded paths).
#
# Expects: NVIS_BIN_SCRIPTS, RACK_CLUSTERKIT_RACK, SLURM_SUBMIT_DIR=log_dir.
# Optional: RACK_CLUSTER_PRODUCT_LINE / RACK_PRODUCT_LINE (mlx/IB: selects core_to_hca_dgxgb200 vs dgxgb300),
#            NVIS_CLUSTERKIT_MAPPER (full path override for --mapper),
#            NVIS_RDMA_LINK_TECH — global default when RACK_CLUSTERKIT_LINK_TECH is unset (auto | mlx | ib | roce | spectrumx | spx).
#            RACK_CLUSTERKIT_LINK_TECH — same values; overrides NVIS_RDMA_LINK_TECH for this job (default auto).
#              auto: mlx when /sys/class/infiniband/mlx5_*/device/local_cpulist is readable; else RoCE / Spectrum-X.
#            RACK_CLUSTERKIT_HCA_PROFILE — preset rail/device table (default empty = built-in default per link family):
#              mlx: gb300_mlx4 (4× mlx5_0..3), b300_mlx8 (8× mlx5_0,1,8..13).
#              roce: gb300_roce_spx_quad (4 aggregated rails), gb300_roce_spx_rail (4 single-plane rails).
#            RACK_CLUSTERKIT_UCX_TLS — optional; RoCE branch defaults to rc,cuda (mlx keeps UCX_TLS=^dc unless set).
#            RACK_CLUSTERKIT_EXTRA_OPTS, CUDA_MAJ / RACK_NVLOOM_CUDA_MAJOR.
# Pairwise NIC tests require ≥2 distinct hosts in the Slurm allocation (same as validation/clusterkit/clusterkit.sh).

#SBATCH --job-name=CLUSTERKIT-rack
#SBATCH --exclusive
#SBATCH --ntasks-per-node=1
#SBATCH --time=04:00:00

module load slurm
cd "${SLURM_SUBMIT_DIR}"

: "${NVIS_BIN_SCRIPTS:?NVIS_BIN_SCRIPTS must point to bin/scripts (set by run_clusterkit.sh)}"
# shellcheck disable=SC1091
. "${NVIS_BIN_SCRIPTS}/nvis-ucx-job-env.inc.sh"
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

chmod a+x "${NVIS_BIN_SCRIPTS}"/core_to_hca_dgxgb200.sh "${NVIS_BIN_SCRIPTS}"/core_to_hca_dgxgb300.sh \
  "${NVIS_BIN_SCRIPTS}"/core_to_hca_clusterkit_roce_numactl.sh 2>/dev/null || true

# shellcheck disable=SC1091
. "${NVIS_BIN_SCRIPTS}/clusterkit-hca-profiles.inc.sh"

LINK_TECH=$(_ck_resolve_link_tech) || exit 1
_ck_apply_hca_profile "${LINK_TECH}" || exit 1

if [[ -n "${NVIS_CLUSTERKIT_MAPPER:-}" ]] && [[ -f "${NVIS_CLUSTERKIT_MAPPER}" ]]; then
  CORE_TO_HCA="${NVIS_CLUSTERKIT_MAPPER}"
elif [[ "${LINK_TECH}" == "roce" ]]; then
  CORE_TO_HCA="${NVIS_BIN_SCRIPTS}/core_to_hca_clusterkit_roce_numactl.sh"
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

_ck_nhosts=$(sort -u "${HOSTFILE}" | grep -cve '^[[:space:]]*$' || echo 0)
if [[ "${_ck_nhosts}" -lt 2 ]]; then
  echo "ERROR: ClusterKit pairwise tests need at least two Slurm nodes; unique hosts in machinefile=${_ck_nhosts}." >&2
  echo "       SLURM_NODELIST=${SLURM_NODELIST:-}  hostfile=${HOSTFILE}" >&2
  echo "       Submit with sbatch -N 2 (or more) / a wider validation target; see run_clusterkit.sh guard." >&2
  exit 1
fi

DATE=$(date "+%Y%m%d_%H%M%S")

if [[ "${LINK_TECH}" == "roce" ]]; then
  export UCX_TLS="${RACK_CLUSTERKIT_UCX_TLS:-rc,cuda}"
else
  export UCX_TLS="${UCX_TLS:-^dc}"
fi
export UCX_LOG_LEVEL="${UCX_LOG_LEVEL:-info}"

readarray -t _CK_ROWS < <(printf '%s' "${CK_HCA_ROWS}" | grep -v '^[[:space:]]*$' || true)
if [[ "${#_CK_ROWS[@]}" -eq 0 ]]; then
  echo "ERROR: no HCA rows after profile resolution (RACK_CLUSTERKIT_HCA_PROFILE / LINK_TECH)." >&2
  exit 1
fi

echo "### ClusterKit rack=${_rack} link_tech=${LINK_TECH} profile=${RACK_CLUSTERKIT_HCA_PROFILE:-<default>} mapper=${CORE_TO_HCA} cwd=${PWD}"
echo "### HPCX_DIR=${HPCX_DIR}"

for idx in "${!_CK_ROWS[@]}"; do
  row="${_CK_ROWS[$idx]}"
  if [[ "${LINK_TECH}" == "roce" ]]; then
    IFS=',' read -r _ck_dev _ck_core _ck_numa _ck_gpu <<< "${row}"
    export CLUSTERKIT_HCA="${_ck_dev}"
    export CORE="${_ck_core}"
    export NUMA="${_ck_numa}"
    export GPU="${_ck_gpu}"
    _hca_tag=$(echo "${CLUSTERKIT_HCA}" | tr '+/' '__' | tr -cd '[:alnum:]._-')
    export HCA_TAG="${_hca_tag}"
    export CK_OUTPUT_SUBDIR="${PWD}/${DATE}_${SLURM_JOB_ID}_${HCA_TAG}"
    # Match validation/clusterkit/clusterkit.sh: Slurm / Open MPI default binding can pin ranks to a
    # single CPU so numactl -C <core> fails ("cpu argument N is out of range"). Clear binding for mapper.
    mpi_opt="-x CK_OUTPUT_SUBDIR -x CLUSTERKIT_HCA -x CORE -x NUMA -x GPU -x SLURM_CPU_BIND=none --bind-to none"
  else
    device="${row}"
    export HCA_TAG="${device}"
    export CLUSTERKIT_HCA="${idx}"
    unset CORE NUMA GPU
    export CK_OUTPUT_SUBDIR="${PWD}/${DATE}_${SLURM_JOB_ID}_${device}"
    mpi_opt="-x CK_OUTPUT_SUBDIR -x CLUSTERKIT_HCA"
  fi
  set -x
  # Optional: export RACK_CLUSTERKIT_EXTRA_OPTS="--foo ..." (space-separated clusterkit.sh flags).
  # shellcheck disable=SC2086
  "${CK_SH}" --mpi_opt "${mpi_opt}" -r "${HPCX_DIR}" -f "${HOSTFILE}" --mapper "${CORE_TO_HCA}" \
    --exe_opt "-d bw -d lat" --unidirectional ${RACK_CLUSTERKIT_EXTRA_OPTS-}
  rc=$?
  set +x
  if [[ "${rc}" -ne 0 ]]; then
    if [[ "${LINK_TECH}" == "roce" ]]; then
      echo "ERROR: clusterkit.sh failed for HCA row ${idx} (${CLUSTERKIT_HCA}), exit ${rc}" >&2
    else
      echo "ERROR: clusterkit.sh failed for HCA index ${idx} (${device}), exit ${rc}" >&2
    fi
    exit "${rc}"
  fi
done

echo "OK: clusterkit finished on ${SLURM_JOB_NODELIST:-?} (data under ${PWD})"
