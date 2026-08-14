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
# ClusterKit mapper for Spectrum-X / RoCE-style ibdev names (no mlx5 sysfs local_cpulist).
# Expects env from clusterkit.sbatch: CLUSTERKIT_HCA, CORE, NUMA, GPU (and UCX_TLS often rc,cuda).

set -euo pipefail

export CUDA_DEVICE_ORDER="${CUDA_DEVICE_ORDER:-PCI_BUS_ID}"

if [[ "${OMPI_COMM_WORLD_LOCAL_SIZE:-1}" -ne 1 ]]; then
  echo "ERROR: core_to_hca_clusterkit_roce_numactl.sh only supports one MPI rank per node (LOCAL_SIZE=${OMPI_COMM_WORLD_LOCAL_SIZE:-})." >&2
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

# Slurm / OMPI may restrict the job cpuset; absolute -C N can fail. Try absolute, then cpuset-relative (+N), then no bind.
if command -v numactl >/dev/null 2>&1; then
  if numactl -C "${CORE}" -m "${NUMA}" -- /bin/true 2>/dev/null; then
    exec numactl -C "${CORE}" -m "${NUMA}" -- "$@"
  fi
  if numactl -C "+${CORE}" -m "${NUMA}" -- /bin/true 2>/dev/null; then
    exec numactl -C "+${CORE}" -m "${NUMA}" -- "$@"
  fi
  echo "WARNING: numactl -C ${CORE} -m ${NUMA} failed (cpuset or topology); running mapper without numactl bind." >&2
  echo "WARNING: Ensure clusterkit.sbatch passes --bind-to none and -x SLURM_CPU_BIND=none, or fix CORE/NUMA in RACK_CLUSTERKIT_HCA_PROFILE." >&2
fi
exec "$@"
