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
#
# RoCE defaults to "detect": the rails come from this node's fabric scan (lib/fabric.inc.sh), kept
# to the cluster's fabric.compute_hca (NVIS_FABRIC_HCA) when it states one, and each rail is paired
# with its own GPU, core and NUMA node by nvis_fabric_rail_map. The fixed presets below name one
# site's devices (roce_r0_p0..) and stay selectable through RACK_CLUSTERKIT_HCA_PROFILE; on a site
# whose devices differ -- NscaleTX-NC runs roce_vf_r1..4 -- they name devices that do not exist.

_nv_ck_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "${_nv_ck_dir}/nvis-rdma-common.inc.sh"

_ck_resolve_link_tech() {
  local raw
  raw="${RACK_CLUSTERKIT_LINK_TECH:-$(nvis_fabric_network)}"
  nvis_rdma_normalize_link_tech_raw "$raw"
}

_ck_profile_family() {
  case "$1" in
    gb300_mlx4 | b300_mlx8) printf mlx ;;
    detect | gb300_roce_spx_quad | gb300_roce_spx_rail | b300_roce_dual_8) printf roce ;;
    *) printf unknown ;;
  esac
}

# RoCE rows from this node's own rails: "rail,core,numa,gpu", one per compute rail.
_ck_detect_rows() {
  command -v nvis_fabric_rail_map >/dev/null 2>&1 || return 1
  local rows
  rows="$(nvis_fabric_rail_map | awk '$2 != "-" {print $1","$4","$3","$2}')" || return 1
  [[ -n "$rows" ]] || return 1
  CK_HCA_ROWS="$rows"
}

_ck_apply_hca_profile() {
  local tech="$1"
  local prof
  prof=$(printf '%s' "${RACK_CLUSTERKIT_HCA_PROFILE:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')

  if [[ -z "$prof" ]]; then
    if [[ "$tech" == "roce" ]]; then
      prof=detect
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
    echo "ERROR: HCA profile '${prof}' is for ${fam}, but link technology is ${tech}. Fix RACK_CLUSTERKIT_HCA_PROFILE or RACK_CLUSTERKIT_LINK_TECH / the cluster's fabric network (NVIS_FABRIC_NETWORK)." >&2
    return 1
  fi

  if [[ "$prof" == detect ]]; then
    if _ck_detect_rows; then
      CK_HCA_PROFILE_USED=detect
      export CK_HCA_ROWS CK_HCA_PROFILE_USED
      return 0
    fi
    echo "WARNING: no RoCE compute rails detected on $(hostname -s 2>/dev/null); falling back to the" \
         "gb300_roce_spx_quad preset (set RACK_CLUSTERKIT_HCA_PROFILE or the cluster's fabric.compute_hca)." >&2
    prof=gb300_roce_spx_quad
  fi
  CK_HCA_PROFILE_USED="$prof"
  export CK_HCA_PROFILE_USED

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
