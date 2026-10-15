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
# Optional: NVIS_CLUSTERKIT_MAPPER (full path override for --mapper; default core_to_hca_rail_map.sh on
#              both fabrics, which binds each rail to the device/GPU/core/NUMA the rail map gave it),
#            NVIS_FABRIC_NETWORK — the cluster's fabric protocol, used when RACK_CLUSTERKIT_LINK_TECH is unset (IB | RoCE | auto | mlx | spectrumx | spx).
#            RACK_CLUSTERKIT_LINK_TECH — same values; overrides NVIS_FABRIC_NETWORK for this job.
#              auto: mlx when /sys/class/infiniband/mlx5_*/device/local_cpulist is readable; else RoCE / Spectrum-X.
#            RACK_CLUSTERKIT_HCA_PROFILE — which rails to sweep. Default on both fabrics: detect -- this
#              node's compute rails from lib/fabric.inc.sh, kept to the cluster's fabric.compute_hca, each
#              with its own GPU/core/NUMA (nvis_fabric_rail_map); none found stops the job and names the
#              setting, never a fallback preset. Presets: mlx -- gb300_mlx4 (mlx5_0..3), b300_mlx8
#              (mlx5_0,1,8..13), device names only, their GPU/core/NUMA from the rail map; roce --
#              gb300_roce_spx_quad (4 aggregated rails), gb300_roce_spx_rail (4 single-plane rails),
#              b300_roce_dual_8.
#            RACK_CLUSTERKIT_UCX_TLS — optional; RoCE branch defaults to rc,cuda (mlx keeps UCX_TLS=^dc unless set).
#            RACK_CLUSTERKIT_TESTS — ClusterKit test cases, space-separated (default "bw lat"); e.g.
#              "gpu_gpu_bw gpu_gpu_lat" with RACK_CLUSTERKIT_EXTRA_OPTS=-G measures each rail from its own GPU.
#            RACK_CLUSTERKIT_GPUDIRECT=1 — the gpu_* tests use GPU-Direct (clusterkit.sh -G).
#            RACK_CLUSTERKIT_SCOPES — "RACK:h1,h2;RACK:h3,h4" (set by a multi-rack launch). Each rack becomes
#              a ClusterKit scope (-S scope file) and every rail runs RACK_CLUSTERKIT_MULTIRACK_TESTS
#              (default "bisection_bw": rack-to-rack bandwidth) instead of the pairwise tests.
#              Both are set per cluster in Projects -> cluster -> ClusterKit.
#            RACK_CLUSTERKIT_EXTRA_OPTS, CUDA_MAJ / RACK_NVLOOM_CUDA_MAJOR.
#            NVIS_EXTRA_ENV_KEYS — names of the cluster's ClusterKit variables (Projects -> cluster ->
#              ClusterKit -> environment, per launch level), each forwarded to every rank with -x.
# A rail whose test fails is reported and the remaining rails still run; the job exits non-zero if any
# failed. (One bad rail used to abort the job and leave the healthy ones unmeasured.)
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

# ClusterKit dlopens the CUDA runtime for its GPU tests (gpu_gpu_bw, ...). hpcx_load does not put it on
# LD_LIBRARY_PATH, and without it ClusterKit prints "skip GPU tests" yet exits 0 with no GPU results.
# mpirun forwards LD_LIBRARY_PATH (-x) to the remote ranks, so setting it here reaches every node.
_ck_cudart="$(nvis_cuda_runtime_libdir)"
if [[ -n "${_ck_cudart}" ]]; then
  export LD_LIBRARY_PATH="${_ck_cudart}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
  echo "### CUDA runtime: ${_ck_cudart}"
else
  echo "WARNING: no libcudart.so.${CUDA_MAJ} under ${WORKSPACE_DIR:-?}/cuda* or /usr/local/cuda*; ClusterKit GPU tests will be skipped (set NVIS_CUDA_LIB)." >&2
fi
if [[ ! -f "${CK_SH}" ]]; then
  echo "ERROR: missing ${CK_SH}" >&2
  exit 1
fi

chmod a+x "${NVIS_BIN_SCRIPTS}"/core_to_hca_rail_map.sh 2>/dev/null || true

# shellcheck disable=SC1091
. "${NVIS_BIN_SCRIPTS}/clusterkit-hca-profiles.inc.sh"

LINK_TECH=$(_ck_resolve_link_tech) || exit 1
_ck_apply_hca_profile "${LINK_TECH}" || exit 1
# Detected rails carry the node's own technology; the run follows them (see _ck_adopt_node_link_tech).
LINK_TECH="${CK_LINK_TECH_DETECTED:-${LINK_TECH}}"

# One mapper for both fabrics: every row carries its rail's device, core, NUMA node and GPU.
CORE_TO_HCA="${NVIS_BIN_SCRIPTS}/core_to_hca_rail_map.sh"
if [[ -n "${NVIS_CLUSTERKIT_MAPPER:-}" ]] && [[ -f "${NVIS_CLUSTERKIT_MAPPER}" ]]; then
  CORE_TO_HCA="${NVIS_CLUSTERKIT_MAPPER}"
fi
# The mapper looks up a routable GID only on RoCE.
export CK_LINK_TECH="${LINK_TECH}"

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

echo "### ClusterKit rack=${_rack} link_tech=${LINK_TECH} profile=${CK_HCA_PROFILE_USED:-${RACK_CLUSTERKIT_HCA_PROFILE:-<default>}} mapper=${CORE_TO_HCA} cwd=${PWD}"
printf '### HCA row: %s\n' "${_CK_ROWS[@]}"
echo "### HPCX_DIR=${HPCX_DIR}"

_ck_exe_opt=""
_ck_tests="${RACK_CLUSTERKIT_TESTS:-bw lat}"
if [[ -n "${RACK_CLUSTERKIT_SCOPES:-}" ]]; then
  # Across racks: write ClusterKit's scope file (host,scope_name,scope_num), one scope per rack.
  _ck_tests="${RACK_CLUSTERKIT_MULTIRACK_TESTS:-bisection_bw}"
  _ck_scope_file="${PWD}/scopes_${SLURM_JOB_ID:-0}.csv"
  : > "${_ck_scope_file}"
  _ck_n=0
  IFS=';' read -ra _ck_parts <<< "${RACK_CLUSTERKIT_SCOPES}"
  for _ck_part in "${_ck_parts[@]}"; do
    [[ -n "${_ck_part}" ]] || continue
    _ck_n=$((_ck_n + 1))
    IFS=',' read -ra _ck_hosts <<< "${_ck_part#*:}"
    for _ck_h in "${_ck_hosts[@]}"; do
      [[ -n "${_ck_h}" ]] || continue
      # Only hosts this job holds: the launcher drops unschedulable nodes, and one left out to make
      # the count even, after the scopes were drawn up.
      if [[ -s "${HOSTFILE:-}" ]] && ! grep -qxF "${_ck_h}" "${HOSTFILE}"; then continue; fi
      printf '%s,%s,%s\n' "${_ck_h}" "${_ck_part%%:*}" "${_ck_n}" >> "${_ck_scope_file}"
    done
  done
  _ck_exe_opt+="-S ${_ck_scope_file} "
fi
for _t in ${_ck_tests}; do _ck_exe_opt+="-d ${_t} "; done
# GPU tests (gpu_* cases, or clusterkit.sh -g) change how the RoCE mapper binds (see it) and what a run
# must produce: ClusterKit skips GPU tests it cannot run yet exits 0, so a rail without GPU results fails.
CK_GPU_TESTS=0
if [[ " ${_ck_tests} " == *" gpu"* || " ${RACK_CLUSTERKIT_EXTRA_OPTS:-} " == *" -g "* ]]; then
  CK_GPU_TESTS=1
fi
export CK_GPU_TESTS
_ck_gdr=""
if [[ "${RACK_CLUSTERKIT_GPUDIRECT:-}" == 1 && " ${RACK_CLUSTERKIT_EXTRA_OPTS:-} " != *" -G "* ]]; then
  _ck_gdr="-G"
fi
_ck_failed=()

# The cluster's ClusterKit variables (Projects -> cluster -> ClusterKit -> environment) arrive in the
# batch env; mpirun hands remote ranks only what -x names, so forward exactly the keys the app lists.
_ck_extra_x=""
for _k in ${NVIS_EXTRA_ENV_KEYS:-}; do
  [[ "${_k}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && _ck_extra_x+=" -x ${_k}"
done
[[ -n "${_ck_extra_x}" ]] && echo "clusterkit: forwarding cluster env to all ranks:${_ck_extra_x// -x/}"

for idx in "${!_CK_ROWS[@]}"; do
  row="${_CK_ROWS[$idx]}"
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
  mpi_opt="-x CK_OUTPUT_SUBDIR -x CLUSTERKIT_HCA -x CORE -x NUMA -x GPU -x CK_GPU_TESTS -x CK_LINK_TECH -x SLURM_CPU_BIND=none --bind-to none${_ck_extra_x}"
  set -x
  # Optional: export RACK_CLUSTERKIT_EXTRA_OPTS="--foo ..." (space-separated clusterkit.sh flags).
  # shellcheck disable=SC2086
  "${CK_SH}" --mpi_opt "${mpi_opt}" -r "${HPCX_DIR}" -f "${HOSTFILE}" --mapper "${CORE_TO_HCA}" \
    --exe_opt "${_ck_exe_opt% }" --unidirectional ${_ck_gdr} ${RACK_CLUSTERKIT_EXTRA_OPTS-}
  rc=$?
  set +x
  if [[ "${rc}" -eq 0 && "${CK_GPU_TESTS}" == 1 ]] && ! compgen -G "${CK_OUTPUT_SUBDIR}/GPU_*.json" >/dev/null; then
    echo "ERROR: ClusterKit reported success for ${HCA_TAG} but wrote no GPU results (it skips GPU tests it cannot run)." >&2
    rc=1
  fi
  if [[ "${rc}" -eq 0 ]] && ! compgen -G "${CK_OUTPUT_SUBDIR}/*.json" >/dev/null; then
    echo "ERROR: ClusterKit reported success for ${HCA_TAG} but wrote no results." >&2
    rc=1
  fi
  if [[ "${rc}" -ne 0 ]]; then
    if [[ "${LINK_TECH}" == "roce" ]]; then
      echo "ERROR: clusterkit.sh failed for HCA row ${idx} (${CLUSTERKIT_HCA}), exit ${rc}" >&2
    else
      echo "ERROR: clusterkit.sh failed for HCA index ${idx} (${device}), exit ${rc}" >&2
    fi
    _ck_failed+=("${HCA_TAG}")
  fi
done

if [[ "${#_ck_failed[@]}" -gt 0 ]]; then
  echo "FAILED: clusterkit could not test ${_ck_failed[*]} on ${SLURM_JOB_NODELIST:-?} (data for the rest under ${PWD})" >&2
  exit 1
fi
echo "OK: clusterkit finished on ${SLURM_JOB_NODELIST:-?} (data under ${PWD})"
