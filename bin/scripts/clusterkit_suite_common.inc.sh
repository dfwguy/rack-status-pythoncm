#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# Shared ClusterKit SU-suite batch body. Sourced by the explicit tier sbatch
# entrypoints; not intended to be called directly.

set -euo pipefail

module load slurm 2>/dev/null || true
cd "${SLURM_SUBMIT_DIR}"

: "${NVIS_BIN_SCRIPTS:?NVIS_BIN_SCRIPTS must point to bin/scripts}"
: "${RACK_CLUSTERKIT_SUITE_OUTDIR:?RACK_CLUSTERKIT_SUITE_OUTDIR is required}"
: "${RACK_CLUSTERKIT_SUITE_TEST_TYPE:?RACK_CLUSTERKIT_SUITE_TEST_TYPE is required}"
: "${RACK_CLUSTERKIT_SUITE_RUN_LABEL:?RACK_CLUSTERKIT_SUITE_RUN_LABEL is required}"

# shellcheck disable=SC1091
. "${NVIS_BIN_SCRIPTS}/nvis-ucx-job-env.inc.sh"
# shellcheck disable=SC1091
. "${NVIS_BIN_SCRIPTS}/workspace-dir-common.sh"

: "${CUDA_MAJ:=${RACK_CLUSTERKIT_CUDA_MAJOR:-${RACK_NVLOOM_CUDA_MAJOR:-13}}}"
export CUDA_MAJ

# shellcheck disable=SC1091
. "${NVIS_BIN_SCRIPTS}/workspace-cuda-major-common.sh"

mkdir -p "${RACK_CLUSTERKIT_SUITE_OUTDIR}"
cd "${RACK_CLUSTERKIT_SUITE_OUTDIR}"

if [[ -n "${NVIS_CLUSTERKIT_HPCX_INIT:-}" && -f "${NVIS_CLUSTERKIT_HPCX_INIT}" ]]; then
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

if [[ -z "${HPCX_DIR:-}" || ! -d "${HPCX_DIR}/clusterkit" ]]; then
  echo "ERROR: clusterkit not found under HPCX_DIR=${HPCX_DIR:-}(after hpcx_load)." >&2
  exit 1
fi

CK_SH="${HPCX_DIR}/clusterkit/bin/clusterkit.sh"
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

if [[ -n "${NVIS_CLUSTERKIT_MAPPER:-}" && -f "${NVIS_CLUSTERKIT_MAPPER}" ]]; then
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
    echo "WARNING: RACK_CLUSTER_PRODUCT_LINE='${_pl_raw}' not recognized; defaulting to core_to_hca_dgxgb200.sh." >&2
    CORE_TO_HCA="${NVIS_BIN_SCRIPTS}/core_to_hca_dgxgb200.sh"
  fi
fi

HOSTFILE="${PWD}/machinefile_${SLURM_JOB_ID}"
scontrol show hostname "${SLURM_NODELIST}" >"${HOSTFILE}"
_ck_nhosts=$(sort -u "${HOSTFILE}" | grep -cve '^[[:space:]]*$' || echo 0)
if [[ "${_ck_nhosts}" -lt 2 ]]; then
  echo "ERROR: ClusterKit pairwise tests need at least two Slurm nodes; unique hosts=${_ck_nhosts}." >&2
  exit 1
fi
if (( _ck_nhosts % 2 != 0 )); then
  echo "ERROR: ClusterKit suite expects an even node count; unique hosts=${_ck_nhosts}." >&2
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
  echo "ERROR: no HCA rows after profile resolution." >&2
  exit 1
fi

echo "### ClusterKit suite=${RACK_CLUSTERKIT_SUITE_SU_LABEL:-?} test=${RACK_CLUSTERKIT_SUITE_TEST_TYPE} label=${RACK_CLUSTERKIT_SUITE_RUN_LABEL} link_tech=${LINK_TECH} profile=${RACK_CLUSTERKIT_HCA_PROFILE:-<default>}"
echo "### HPCX_DIR=${HPCX_DIR}"
echo "### HOSTS=${_ck_nhosts} SLURM_NODELIST=${SLURM_NODELIST:-?}"

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
  # shellcheck disable=SC2086
  "${CK_SH}" --mpi_opt "${mpi_opt}" -r "${HPCX_DIR}" -f "${HOSTFILE}" --mapper "${CORE_TO_HCA}" \
    --exe_opt "-d bw -d lat" --unidirectional ${RACK_CLUSTERKIT_EXTRA_OPTS-}
  rc=$?
  set +x
  if [[ "${rc}" -ne 0 ]]; then
    echo "ERROR: clusterkit.sh failed for HCA ${HCA_TAG}, exit ${rc}" >&2
    exit "${rc}"
  fi
done

echo "OK: clusterkit suite test=${RACK_CLUSTERKIT_SUITE_TEST_TYPE} label=${RACK_CLUSTERKIT_SUITE_RUN_LABEL} finished on ${SLURM_JOB_NODELIST:-?} (data under ${PWD})"
