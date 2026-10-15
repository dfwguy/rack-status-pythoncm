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
# ClusterKit mapper for one rail, on InfiniBand or RoCE: bind the rank to the rail's device, GPU,
# core and NUMA node. clusterkit.sbatch hands it one row of nvis_fabric_rail_map (clusterkit-hca-
# profiles.inc.sh) as CLUSTERKIT_HCA, CORE, NUMA, GPU, plus CK_LINK_TECH (mlx | roce). This replaced
# core_to_hca_dgxgb200/300.sh, which held a fixed mlx5_N device and CPU range per local rank and so
# described only the hardware they were written on.

set -euo pipefail

export CUDA_DEVICE_ORDER="${CUDA_DEVICE_ORDER:-PCI_BUS_ID}"

if [[ "${OMPI_COMM_WORLD_LOCAL_SIZE:-1}" -ne 1 ]]; then
  echo "ERROR: core_to_hca_rail_map.sh only supports one MPI rank per node (LOCAL_SIZE=${OMPI_COMM_WORLD_LOCAL_SIZE:-})." >&2
  exit 1
fi

: "${CLUSTERKIT_HCA:?CLUSTERKIT_HCA must be set}"
: "${CORE:?CORE must be set}"
: "${NUMA:?NUMA must be set}"
: "${GPU:?GPU must be set}"

if printf '%s' "${CLUSTERKIT_HCA}" | grep -q '+'; then
  _ucx_nd=$(
    printf '%s' "${CLUSTERKIT_HCA}" | tr '+' '\n' | sed '/^$/d' | while read -r dev; do
      printf '%s:1\n' "${dev}"
    done | paste -sd, -
  )
  export UCX_NET_DEVICES="${_ucx_nd}"
  export UCX_MAX_RNDV_RAILS="${UCX_MAX_RNDV_RAILS:-4}"
else
  export UCX_NET_DEVICES="${CLUSTERKIT_HCA}:1"
fi

export CUDA_VISIBLE_DEVICES="${GPU}"

# RoCE only: reach the other rack through the rail's routable GID, looked up on this node (the index
# differs per node). UCX's automatic pick can be a link-local RoCE v2 GID, and then MPI's own address
# exchange times out before ClusterKit measures anything. InfiniBand addresses peers by LID and has
# no routable GID to find. A caller-set UCX_IB_GID_INDEX wins.
if [[ "${CK_LINK_TECH:-roce}" == roce && -z "${UCX_IB_GID_INDEX:-}" ]]; then
  _ck_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/fabric.inc.sh"
  if [[ -f "${_ck_lib}" ]]; then
    # shellcheck disable=SC1090
    . "${_ck_lib}"
    _ck_gid="$(nvis_fabric_routable_gid_index "${CLUSTERKIT_HCA%%+*}")"
    if [[ -n "${_ck_gid}" ]]; then
      export UCX_IB_GID_INDEX="${_ck_gid}"
    else
      echo "WARNING: no routable RoCE v2 GID on ${CLUSTERKIT_HCA%%+*}; leaving UCX to choose (cross-rack may time out)." >&2
    fi
  fi
fi

# GPU tests: bind memory to the rail's NUMA node but do not pin the CPU. On GB300 (NscaleTX-NC,
# 2026-10-12) a CUDA process pinned to any CPU of the second socket -- one core or the rail's whole
# local_cpulist -- ran no GPU tests at all (ClusterKit skipped them and still exited 0), while
# memory-only binding measured ~700 Gb/s from either socket. Host-memory tests keep the core pin.
if [[ "${CK_GPU_TESTS:-0}" == 1 ]] && command -v numactl >/dev/null 2>&1; then
  if numactl -m "${NUMA}" -- /bin/true 2>/dev/null; then
    exec numactl -m "${NUMA}" -- "$@"
  fi
  exec "$@"
fi

# Slurm / OMPI may restrict the job cpuset; absolute -C N can fail. Try absolute, then cpuset-relative (+N), then no bind.
if command -v numactl >/dev/null 2>&1; then
  if numactl -C "${CORE}" -m "${NUMA}" -- /bin/true 2>/dev/null; then
    exec numactl -C "${CORE}" -m "${NUMA}" -- "$@"
  fi
  if numactl -C "+${CORE}" -m "${NUMA}" -- /bin/true 2>/dev/null; then
    exec numactl -C "+${CORE}" -m "${NUMA}" -- "$@"
  fi
  echo "WARNING: numactl -C ${CORE} -m ${NUMA} failed (cpuset or topology); running mapper without numactl bind." >&2
  echo "WARNING: Ensure clusterkit.sbatch passes --bind-to none and -x SLURM_CPU_BIND=none, or check the rail map (fabric.compute_hca / RACK_CLUSTERKIT_HCA_PROFILE)." >&2
fi
exec "$@"
