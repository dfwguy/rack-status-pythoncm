# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# shellcheck shell=bash
# ClusterKit HCA presets: sourced by clusterkit.sbatch after nvis-rdma-common.inc.sh.
# Exports CK_HCA_ROWS (newline-separated). RoCE rows are CSV: HCA_DEVICES,CORE,NUMA,GPU
# where HCA_DEVICES may be multi-rail "dev1+dev2+...".

_nv_ck_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "${_nv_ck_dir}/nvis-rdma-common.inc.sh"

_ck_resolve_link_tech() {
  local raw
  raw="${RACK_CLUSTERKIT_LINK_TECH:-${NVIS_RDMA_LINK_TECH:-auto}}"
  nvis_rdma_normalize_link_tech_raw "$raw"
}

_ck_profile_family() {
  case "$1" in
    gb300_mlx4 | b300_mlx8) printf mlx ;;
    gb300_roce_spx_quad | gb300_roce_spx_rail | b300_roce_dual_8) printf roce ;;
    *) printf unknown ;;
  esac
}

_ck_apply_hca_profile() {
  local tech="$1"
  local prof
  prof=$(printf '%s' "${RACK_CLUSTERKIT_HCA_PROFILE:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')

  if [[ -z "$prof" ]]; then
    if [[ "$tech" == "roce" ]]; then
      prof=gb300_roce_spx_quad
    else
      prof=gb300_mlx4
    fi
  fi

  local fam
  fam=$(_ck_profile_family "$prof")
  if [[ "$fam" == "unknown" ]]; then
    echo "ERROR: unknown RACK_CLUSTERKIT_HCA_PROFILE='${RACK_CLUSTERKIT_HCA_PROFILE}' (see clusterkit.sbatch header)." >&2
    return 1
  fi
  if [[ "$fam" != "$tech" ]]; then
    echo "ERROR: HCA profile '${prof}' is for ${fam}, but link technology is ${tech}. Fix RACK_CLUSTERKIT_HCA_PROFILE or RACK_CLUSTERKIT_LINK_TECH / NVIS_RDMA_LINK_TECH." >&2
    return 1
  fi

  case "$prof" in
    gb300_mlx4)
      CK_HCA_ROWS=$'mlx5_0\nmlx5_1\nmlx5_2\nmlx5_3'
      ;;
    b300_mlx8)
      CK_HCA_ROWS=$'mlx5_0\nmlx5_1\nmlx5_8\nmlx5_9\nmlx5_10\nmlx5_11\nmlx5_12\nmlx5_13'
      ;;
    gb300_roce_spx_quad)
      CK_HCA_ROWS=$'roce_r0_p0+roce_r0_p1+roce_r0_p2+roce_r0_p3,1,0,1\nroce_r1_p0+roce_r1_p1+roce_r1_p2+roce_r1_p3,2,0,0\nroce_r2_p0+roce_r2_p1+roce_r2_p2+roce_r2_p3,73,1,3\nroce_r3_p0+roce_r3_p1+roce_r3_p2+roce_r3_p3,74,1,2'
      ;;
    gb300_roce_spx_rail)
      CK_HCA_ROWS=$'roce_r0_p0,1,0,1\nroce_r1_p0,2,0,0\nroce_r2_p0,73,1,3\nroce_r3_p0,74,1,2'
      ;;
    b300_roce_dual_8)
      CK_HCA_ROWS=$'roce_p0_r1+roce_p1_r1,1,0,0\nroce_p0_r2+roce_p1_r2,2,0,1\nroce_p0_r0+roce_p1_r0,3,0,2\nroce_p0_r3+roce_p1_r3,4,0,3\nroce_p0_r4+roce_p1_r4,87,1,4\nroce_p0_r7+roce_p1_r7,88,1,5\nroce_p0_r5+roce_p1_r5,89,1,6\nroce_p0_r6+roce_p1_r6,90,1,7'
      ;;
    *)
      echo "ERROR: unhandled profile '${prof}'." >&2
      return 1
      ;;
  esac
  export CK_HCA_ROWS
}
