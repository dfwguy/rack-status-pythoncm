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

# map DGX-GB200 HCA to core, so that processes have affinity to the HCA they are assigned.
# Assume we only run # of ranks that corresponds to the number of HCAs

#
# Use the following command to find core values to map:
#
# for mlx in `ls /sys/class/infiniband/mlx5_*/device/local_cpulist`; do echo -n "$mlx:  "; cat $mlx; done
#

_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${_script_dir}/nvis-rdma-common.inc.sh" ]]; then
  # shellcheck disable=SC1091
  . "${_script_dir}/nvis-rdma-common.inc.sh"
  _rdma_tech=$(nvis_rdma_normalize_link_tech_raw "${RACK_CLUSTERKIT_LINK_TECH:-${NVIS_RDMA_LINK_TECH:-auto}}") || exit 1
  if [[ "${_rdma_tech}" == "roce" ]]; then
    echo "ERROR: core_to_hca_dgxgb200.sh expects InfiniBand mlx5 HCAs with sysfs local_cpulist, but link technology resolved to RoCE/Spectrum-X." >&2
    echo "       Use ClusterKit with RACK_CLUSTERKIT_LINK_TECH=roce (or NVIS_RDMA_LINK_TECH) and a RoCE HCA profile, or set NVIS_CLUSTERKIT_MAPPER to core_to_hca_clusterkit_roce_numactl.sh." >&2
    exit 1
  fi
fi

if [ "${OMPI_COMM_WORLD_LOCAL_SIZE}" -eq 1 ]; then
  export UCX_NET_DEVICES=${HCA_TAG}:1
  taskset -c "$(cat "/sys/class/infiniband/${HCA_TAG}/device/local_cpulist")" "$@"
else
  case $OMPI_COMM_WORLD_LOCAL_RANK in
        # ------------------ CX7 ----------------------------
        0) core=0-71;    export UCX_NET_DEVICES=mlx5_0:1 ;;
        1) core=0-71;    export UCX_NET_DEVICES=mlx5_1:1 ;;
        # ------------------ BF3 ----------------------------
        2) core=0-71;    export UCX_NET_DEVICES=mlx5_2:1 ;;
        3) core=0-71;    export UCX_NET_DEVICES=mlx5_3:1 ;;
        # ------------------ CX7 ----------------------------
        4) core=72-143;  export UCX_NET_DEVICES=mlx5_4:1 ;;
        5) core=72-143;  export UCX_NET_DEVICES=mlx5_5:1 ;;
        # ------------------ BF3 ----------------------------
        6) core=72-143;  export UCX_NET_DEVICES=mlx5_6:1 ;;
        7) core=72-143;  export UCX_NET_DEVICES=mlx5_7:1 ;;
  esac

  taskset -c "$core" "$@"
fi
