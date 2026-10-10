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
# Shared RDMA / interconnect helpers for NVIS scripts (source, do not execute).
#
# NVIS_FABRIC_NETWORK — the cluster's fabric protocol: IB | RoCE, or auto (default) | mlx | infiniband |
#   spectrumx | spx. auto: use classic IB mlx5 sysfs CPU affinity when readable; otherwise treat as
#   RoCE / Spectrum-X style. The deprecated NVIS_RDMA_LINK_TECH is read only when it is unset.
#
# Typical usage from another script:
#   _NVIS_SCR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   # shellcheck disable=SC1091
#   . "${_NVIS_SCR}/nvis-rdma-common.inc.sh"
#   export UCX_NET_DEVICES
#   UCX_NET_DEVICES="$(nvis_rdma_default_ucx_net_devices)" || exit 1
#
# This shim now delegates mlx5 rail selection to the shared classifier in
# lib/fabric.inc.sh (ACTIVE + InfiniBand + top line rate, so slower BlueField-3
# storage NICs are excluded). If that include is unavailable or finds nothing, it
# falls back to the legacy "all mlx5_*" listing so existing callers never regress.

# Best-effort: pull in the shared fabric classifier from lib/fabric.inc.sh.
_NVIS_RDMA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || _NVIS_RDMA_DIR=""
if [[ -n "${_NVIS_RDMA_DIR}" && -f "${_NVIS_RDMA_DIR}/lib/fabric.inc.sh" ]]; then
  # shellcheck disable=SC1091
  . "${_NVIS_RDMA_DIR}/lib/fabric.inc.sh"
fi
# The cluster's fabric protocol comes from lib/fabric.inc.sh; where that include is absent, the
# same reading of the same variables, so every script resolves it one way.
if ! declare -F nvis_fabric_network >/dev/null 2>&1; then
  nvis_fabric_network() { printf '%s' "${NVIS_FABRIC_NETWORK:-${NVIS_RDMA_LINK_TECH:-auto}}"; }
fi

# True when at least one mlx5_* device exposes a non-empty local_cpulist (DGX IB-style).
nvis_rdma_mlx_infiniband_usable() {
  local d f
  shopt -s nullglob
  for d in /sys/class/infiniband/mlx5_*; do
    [[ -d "$d" ]] || continue
    f="${d}/device/local_cpulist"
    if [[ -r "$f" ]] && grep -q '[^[:space:]]' "$f" 2>/dev/null; then
      shopt -u nullglob
      return 0
    fi
  done
  shopt -u nullglob
  return 1
}

# Print mlx or roce (stdout). On invalid input, print to stderr and return 1.
# Argument: raw token (e.g. the value of nvis_fabric_network).
nvis_rdma_normalize_link_tech_raw() {
  local raw up
  raw="${1:-auto}"
  raw="${raw//[[:space:]]/}"
  [[ -z "$raw" ]] && raw=auto
  up=$(printf '%s' "$raw" | tr '[:lower:]' '[:upper:]')
  case "$up" in
    AUTO)
      if nvis_rdma_mlx_infiniband_usable; then
        printf mlx
      else
        printf roce
      fi
      ;;
    MLX | IB | INFINIBAND)
      printf mlx
      ;;
    ROCE | SPECTRUMX | SPX)
      printf roce
      ;;
    *)
      echo "ERROR: invalid RDMA link technology '${1}' (use auto, mlx, ib, roce, spectrumx, spx)." >&2
      return 1
      ;;
  esac
}

# Space-separated ibdev names (no :port) for UCX_NET_DEVICES construction.
nvis_rdma_list_ibdevs_for_mode() {
  local tech
  tech=$(nvis_rdma_normalize_link_tech_raw "$(nvis_fabric_network)") || return 1
  if [[ "$tech" == "mlx" ]]; then
    nvis_rdma_list_mlx_ibdevs || return 1
  else
    nvis_rdma_list_roce_ibdevs || return 1
  fi
}

nvis_rdma_list_mlx_ibdevs() {
  # Prefer the shared classifier: ACTIVE + InfiniBand + top rate (drops BF3 storage).
  if declare -F nvis_fabric_scan >/dev/null 2>&1; then
    local _scan _hca
    _scan="$(NVIS_FABRIC_NETWORK=IB nvis_fabric_scan 2>/dev/null)"
    _hca="$(printf '%s\n' "${_scan}" | sed -n 's/^NVIS_FAB_IB_HCA=//p')"
    if [[ -n "${_hca}" ]]; then
      printf '%s' "${_hca//,/ }"
      return 0
    fi
  fi
  # Fallback (legacy): every mlx5_* device under sysfs.
  local d out=()
  shopt -s nullglob
  for d in /sys/class/infiniband/mlx5_*; do
    [[ -d "$d" ]] || continue
    out+=("$(basename "$d")")
  done
  shopt -u nullglob
  if [[ "${#out[@]}" -eq 0 ]]; then
    echo "ERROR: no mlx5_* devices under /sys/class/infiniband." >&2
    return 1
  fi
  printf '%s' "${out[*]}"
}

nvis_rdma_list_roce_ibdevs() {
  local d out=()
  shopt -s nullglob
  for d in /sys/class/infiniband/roce_*; do
    [[ -e "$d" ]] || continue
    out+=("$(basename "$d")")
  done
  shopt -u nullglob
  if [[ "${#out[@]}" -gt 0 ]]; then
    printf '%s' "${out[*]}"
    return 0
  fi
  # Fallback: Mellanox HCAs that are not mlx5_* naming (rare); exclude obvious bonds.
  for d in /sys/class/infiniband/*; do
    [[ -e "$d" ]] || continue
    [[ -d "$d" ]] || continue
    local b
    b=$(basename "$d")
    [[ "$b" == mlx5_* ]] && continue
    [[ "$b" == bond_* ]] && continue
    out+=("$b")
  done
  if [[ "${#out[@]}" -eq 0 ]]; then
    echo "ERROR: no RoCE-style ibdevs found under /sys/class/infiniband (expected roce_* or other HCAs)." >&2
    return 1
  fi
  printf '%s' "${out[*]}"
}

# Comma-separated dev:1 list suitable for UCX_NET_DEVICES (best-effort for multirail).
nvis_rdma_default_ucx_net_devices() {
  local tech
  tech=$(nvis_rdma_normalize_link_tech_raw "$(nvis_fabric_network)") || return 1
  local -a devs=()
  read -r -a devs <<<"$(nvis_rdma_list_ibdevs_for_mode)" || return 1
  local out="" d
  for d in "${devs[@]}"; do
    [[ -z "$d" ]] && continue
    [[ -n "$out" ]] && out+=","
    out+="${d}:1"
  done
  printf '%s' "$out"
}
