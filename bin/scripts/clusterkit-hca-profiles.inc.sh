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
# ClusterKit HCA rows: sourced by clusterkit.sbatch after nvis-rdma-common.inc.sh.
# Exports CK_HCA_ROWS (newline-separated) and CK_HCA_PROFILE_USED. Every row, IB or RoCE, is CSV:
# HCA_DEVICES,CORE,NUMA,GPU -- HCA_DEVICES may be multi-rail "dev1+dev2+..." (RoCE presets only).
#
# The default on both fabrics is "detect": the rails come from this node's fabric scan
# (lib/fabric.inc.sh), kept to the cluster's fabric.compute_hca (NVIS_FABRIC_HCA) when it states one,
# and each rail is paired with its own GPU, core and NUMA node by nvis_fabric_rail_map. When nothing
# is detected the job stops and names the setting to state; it never falls back to a preset, because
# a preset names one site's devices (roce_r0_p0.., mlx5_0..3) and on another site -- NscaleTX-NC runs
# roce_vf_r1..4 -- it measures devices that do not exist, or the wrong ones.
#
# Presets stay selectable through RACK_CLUSTERKIT_HCA_PROFILE. The IB ones list device NAMES only;
# their core, NUMA node and GPU come from the rail map like detected rails, so no CPU range or GPU
# index is written down for hardware this file has never seen. A preset device the node lacks is an
# error, not a silently shorter sweep.

_nv_ck_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "${_nv_ck_dir}/nvis-rdma-common.inc.sh"

_ck_resolve_link_tech() {
  local raw
  raw="${RACK_CLUSTERKIT_LINK_TECH:-$(nvis_fabric_network)}"
  nvis_rdma_normalize_link_tech_raw "$raw"
}

# detect works on either fabric; a preset belongs to one.
_ck_profile_family() {
  case "$1" in
    detect) printf any ;;
    gb300_mlx4 | b300_mlx8) printf mlx ;;
    gb300_roce_spx_quad | gb300_roce_spx_rail | b300_roce_dual_8) printf roce ;;
    *) printf unknown ;;
  esac
}

# Rows "rail,core,numa,gpu" from the rail map. No arguments: this node's own compute rails. With
# arguments: exactly those devices, each of which must exist and map to a GPU.
_ck_rows_from_map() {
  command -v nvis_fabric_rail_map >/dev/null 2>&1 || return 1
  local map rows dev missing=()
  if (($#)); then
    for dev in "$@"; do
      [[ -e "${NVIS_FABRIC_SYSFS:-/sys/class/infiniband}/${dev}" ]] || missing+=("$dev")
    done
    if ((${#missing[@]})); then
      echo "ERROR: HCA profile '${CK_HCA_PROFILE_USED}' names device(s) this node does not have: ${missing[*]}" >&2
      return 1
    fi
  fi
  map="$(nvis_fabric_rail_map "$@")" || return 1
  rows="$(awk '$2 != "-" {print $1","$4","$3","$2}' <<<"$map")"
  if (($#)) && [[ "$(grep -c . <<<"$rows")" -ne $# ]]; then
    echo "ERROR: HCA profile '${CK_HCA_PROFILE_USED}': not every device maps to a GPU on this node:" >&2
    printf '  %s\n' "$map" >&2
    return 1
  fi
  [[ -n "$rows" ]] || return 1
  CK_HCA_ROWS="$rows"
}

# Detected rails are the node's own, so their technology is too: when it differs from the stated one
# (a cluster or RACK_CLUSTERKIT_LINK_TECH saying IB on a Spectrum-X node), the run must follow the
# rails -- RoCE needs its routable GID and RoCE transports, IB neither. Sets CK_LINK_TECH_DETECTED,
# which clusterkit.sbatch adopts as its LINK_TECH, and says so.
_ck_adopt_node_link_tech() {
  local stated="$1" NVIS_FAB_LINK="" node
  eval "$(nvis_fabric_scan 2>/dev/null | grep '^NVIS_FAB_LINK=')"
  case "$NVIS_FAB_LINK" in ib) node=mlx ;; roce) node=roce ;; *) return 0 ;; esac
  [[ "$node" == "$stated" ]] && return 0
  echo "WARNING: link technology is ${stated} but this node's compute rails are ${node}; running as ${node}" \
       "-- correct the cluster's fabric network (fabric.scaleout / NVIS_FABRIC_NETWORK) or RACK_CLUSTERKIT_LINK_TECH." >&2
  CK_LINK_TECH_DETECTED="$node"
  export CK_LINK_TECH_DETECTED
}

_ck_apply_hca_profile() {
  local tech="$1"
  local prof
  unset CK_LINK_TECH_DETECTED
  prof=$(printf '%s' "${RACK_CLUSTERKIT_HCA_PROFILE:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
  [[ -n "$prof" ]] || prof=detect

  local fam
  fam=$(_ck_profile_family "$prof")
  if [[ "$fam" == "unknown" ]]; then
    echo "ERROR: unknown RACK_CLUSTERKIT_HCA_PROFILE='${RACK_CLUSTERKIT_HCA_PROFILE}' (see clusterkit.sbatch header)." >&2
    return 1
  fi
  if [[ "$fam" != any && "$fam" != "$tech" ]]; then
    echo "ERROR: HCA profile '${prof}' is for ${fam}, but link technology is ${tech}. Fix RACK_CLUSTERKIT_HCA_PROFILE or RACK_CLUSTERKIT_LINK_TECH / the cluster's fabric network (NVIS_FABRIC_NETWORK)." >&2
    return 1
  fi
  CK_HCA_PROFILE_USED="$prof"
  export CK_HCA_PROFILE_USED

  case "$prof" in
    detect)
      if ! _ck_rows_from_map; then
        echo "ERROR: no ${tech} compute rails detected on $(hostname -s 2>/dev/null); ClusterKit will not guess" \
             "another site's devices. State the rails in Projects -> cluster -> Fabric -> compute HCA" \
             "(fabric.compute_hca, now NVIS_FABRIC_HCA='${NVIS_FABRIC_HCA:-}'), check the cluster's fabric" \
             "network (NVIS_FABRIC_NETWORK='${NVIS_FABRIC_NETWORK:-}'), or name a preset with RACK_CLUSTERKIT_HCA_PROFILE." >&2
        return 1
      fi
      _ck_adopt_node_link_tech "$tech"
      ;;
    gb300_mlx4)
      _ck_rows_from_map mlx5_0 mlx5_1 mlx5_2 mlx5_3 || return 1
      ;;
    b300_mlx8)
      _ck_rows_from_map mlx5_0 mlx5_1 mlx5_8 mlx5_9 mlx5_10 mlx5_11 mlx5_12 mlx5_13 || return 1
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
