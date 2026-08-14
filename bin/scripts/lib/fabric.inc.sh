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
# lib/fabric.inc.sh — platform-agnostic fabric (NIC) classifier + env wireup.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Source (do not execute). Classifies the RDMA devices on a node into the COMPUTE
# rails (the plane that carries GPU collectives cross-rack) vs the storage / NS /
# mgmt plane, then exports the NCCL / UCX / Open MPI env so collectives ride the
# correct plane. This is the generic, reusable form of the "wrong-NIC" fix
# (NCCL had auto-selected a BlueField-3 RoCE port on an unroutable /31 instead of
# the 400G InfiniBand rails).
#
# Selection heuristic (works across GB200/GB300/B300/VR200/HGX + IB or RoCE):
#   1. Consider only mlx5_* ports that are state=ACTIVE.
#   2. Split by link_layer: InfiniBand vs Ethernet(RoCE).
#   3. Pick the desired technology (NVIS_RDMA_LINK_TECH: ib|roce|auto; auto = IB
#      if any active IB port exists, else RoCE).
#   4. Within that technology keep only ports at the TOP link rate — this drops
#      slower BlueField-3 storage NICs (e.g. 200G) while keeping the 400G/800G
#      compute rails.
#   5. Sanity gate: if the count of selected rails is < the GPU count we log a
#      warning (likely mis-detection) but still proceed with what we found.
#
# EVERY exported var is overridable: if the caller already set it (site override,
# platform tool_param_defaults forwarded from the app), we keep their value.
#
# Public functions:
#   nvis_fabric_scan            — print eval-able NVIS_FAB_* assignments (local sysfs)
#   nvis_fabric_export          — scan locally + export NCCL/UCX/OMPI env (on-node use)
#   nvis_fabric_export_from_node <node> [ssh]  — scan a remote node + export (head/submit-time use)
#   nvis_fabric_report          — human-readable classification (+ expected-vs-actual if NVIS_FABRIC_EXPECT_*)
#
# Env inputs:
#   NVIS_RDMA_LINK_TECH   ib | roce | auto      (default auto)
#   GPUS_PER_NODE         gpu count for the gate (default: nvidia-smi -L count, else 4)
#   NVIS_FABRIC_NS_IFNAME bootstrap/NS netdev override (default: bond0 if up, else first up eth)
#   NVIS_FABRIC_EXPECT_RAILS / NVIS_FABRIC_EXPECT_RATE  optional expected profile (validation)

# --- local sysfs scan -------------------------------------------------------
# Prints (to stdout) eval-able assignments:
#   NVIS_FAB_LINK=ib|roce  NVIS_FAB_RATE=<gbps>  NVIS_FAB_RAILS=<n>
#   NVIS_FAB_IB_HCA=mlx5_0,mlx5_1,...   NVIS_FAB_UCX_NET_DEVICES=mlx5_0:1,...
#   NVIS_FAB_NS_IFNAME=<netdev>
nvis_fabric_scan() {
  local want="${NVIS_RDMA_LINK_TECH:-auto}"
  want="$(printf '%s' "${want}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  [[ "${want}" == "mlx" ]] && want=ib
  [[ "${want}" == "spectrumx" || "${want}" == "spx" ]] && want=roce

  local ibroot="${NVIS_FABRIC_SYSFS:-/sys/class/infiniband}"
  local d dev ll st rate rnum
  local -a ib=() eth=()
  local top_ib=0 top_eth=0
  shopt -s nullglob
  for d in "${ibroot}"/mlx5_*; do
    [[ -d "$d" ]] || continue
    dev="$(basename "$d")"
    st="$(cat "$d/ports/1/state" 2>/dev/null)"
    printf '%s' "$st" | grep -q 'ACTIVE' || continue
    ll="$(cat "$d/ports/1/link_layer" 2>/dev/null)"
    rate="$(cat "$d/ports/1/rate" 2>/dev/null)"
    rnum="$(printf '%s' "$rate" | grep -oE '[0-9]+' | head -1)"; rnum="${rnum:-0}"
    case "$ll" in
      InfiniBand) ib+=("${dev}:${rnum}");  (( rnum > top_ib ))  && top_ib=$rnum ;;
      Ethernet)   eth+=("${dev}:${rnum}"); (( rnum > top_eth )) && top_eth=$rnum ;;
    esac
  done
  shopt -u nullglob

  # Resolve technology (auto -> prefer IB when present).
  local link
  case "$want" in
    ib)   link=ib ;;
    roce) link=roce ;;
    *)    if [[ "${#ib[@]}" -gt 0 ]]; then link=ib; else link=roce; fi ;;
  esac

  local -a pool; local top
  if [[ "$link" == "ib" ]]; then pool=("${ib[@]}"); top=$top_ib; else pool=("${eth[@]}"); top=$top_eth; fi

  # Keep only devices at the top rate (drops slower storage/BF3 NICs).
  local -a rails=(); local e name r
  for e in "${pool[@]}"; do
    name="${e%%:*}"; r="${e##*:}"
    [[ "$r" == "$top" ]] && rails+=("$name")
  done

  local hca ucx
  hca="$(IFS=,; echo "${rails[*]}")"
  ucx="$(printf '%s\n' "${rails[@]}" | sed 's/$/:1/' | paste -sd, -)"

  printf 'NVIS_FAB_LINK=%s\n' "$link"
  printf 'NVIS_FAB_RATE=%s\n' "$top"
  printf 'NVIS_FAB_RAILS=%s\n' "${#rails[@]}"
  printf 'NVIS_FAB_IB_HCA=%s\n' "$hca"
  printf 'NVIS_FAB_UCX_NET_DEVICES=%s\n' "$ucx"
  printf 'NVIS_FAB_NS_IFNAME=%s\n' "$(_nvis_fabric_ns_ifname)"
  printf 'NVIS_FAB_GPUS=%s\n' "$(_nvis_fabric_gpu_count)"
}

# Pick the routable bootstrap / north-south netdev.
_nvis_fabric_ns_ifname() {
  if [[ -n "${NVIS_FABRIC_NS_IFNAME:-}" ]]; then printf '%s' "${NVIS_FABRIC_NS_IFNAME}"; return; fi
  local netroot="${NVIS_FABRIC_NETFS:-/sys/class/net}"
  local nic st
  for nic in bond0 bond1; do
    if [[ -r "${netroot}/${nic}/operstate" ]]; then
      st="$(tr -d '\n' < "${netroot}/${nic}/operstate")"
      [[ "$st" == "up" ]] && { printf '%s' "$nic"; return; }
    fi
  done
  # First "up" ethernet-ish netdev that isn't loopback/docker/ib/veth.
  local n
  for n in "${netroot}"/*; do
    nic="$(basename "$n")"
    case "$nic" in lo|docker*|veth*|ib*|virbr*) continue ;; esac
    [[ -r "$n/operstate" ]] || continue
    [[ "$(tr -d '\n' < "$n/operstate")" == "up" ]] && { printf '%s' "$nic"; return; }
  done
  printf 'bond0'  # last-resort default
}

# Number of local GPUs (for the sanity gate).
_nvis_fabric_gpu_count() {
  if [[ -n "${GPUS_PER_NODE:-}" && "${GPUS_PER_NODE}" -gt 0 ]] 2>/dev/null; then
    printf '%s' "${GPUS_PER_NODE}"; return
  fi
  local n; n="$(nvidia-smi -L 2>/dev/null | grep -c '^GPU')" || n=0
  [[ "${n:-0}" -gt 0 ]] && { printf '%s' "$n"; return; }
  printf '4'
}

# --- export NCCL / UCX / OMPI env from a scan (respects existing overrides) --
# $1..: optional pre-captured NVIS_FAB_* eval lines; if empty, scans locally.
_nvis_fabric_apply() {
  local scan="$1"
  # shellcheck disable=SC2086
  eval "${scan}"

  local gpus; gpus="$(_nvis_fabric_gpu_count)"
  if [[ -n "${NVIS_FAB_RAILS:-}" && "${NVIS_FAB_RAILS}" -gt 0 && "${NVIS_FAB_RAILS}" -lt "${gpus}" ]]; then
    echo "nvis_fabric: WARNING: only ${NVIS_FAB_RAILS} compute rail(s) found for ${gpus} GPUs" \
         "(link=${NVIS_FAB_LINK} rate=${NVIS_FAB_RATE}G) — check NVIS_RDMA_LINK_TECH / cabling" >&2
  fi
  if [[ -z "${NVIS_FAB_IB_HCA:-}" ]]; then
    echo "nvis_fabric: WARNING: no compute rails detected; leaving NCCL/UCX env untouched" >&2
    return 0
  fi

  # Compute-plane device selection.
  #
  # Rail NAMES are per-node facts, not rack facts. A scan performed on one node (or on
  # rank 0 of a batch job) must NOT be broadcast to the others: on a rack where one tray
  # is missing a rail, pinning the scanning node's list makes NCCL open a device that does
  # not exist there. Job 65648 died exactly this way -- NCCL_IB_HCA=mlx5_0..3 forced onto
  # h03-12 (no mlx5_2) and h03-37 (no mlx5_3) ->
  #   "Detected mixed local CollNet device counts across ranks (min 3, max 4)".
  #
  # Default is therefore AUTO: export nothing device-specific and let every rank discover
  # its own HCAs locally. Set NVIS_FABRIC_PIN_DEVICES=1 to restore the old pinned behaviour
  # (single-node/homogeneous runs only). An explicit NCCL_IB_HCA/UCX_NET_DEVICES from the
  # operator is always honoured, pinning on or off.
  if [[ "${NVIS_FABRIC_PIN_DEVICES:-0}" == "1" ]]; then
    export NCCL_IB_HCA="${NCCL_IB_HCA:-${NVIS_FAB_IB_HCA}}"
    export UCX_NET_DEVICES="${UCX_NET_DEVICES:-${NVIS_FAB_UCX_NET_DEVICES}}"
  else
    [[ -n "${NCCL_IB_HCA:-}" ]] && export NCCL_IB_HCA
    [[ -n "${UCX_NET_DEVICES:-}" ]] && export UCX_NET_DEVICES
  fi
  # Bootstrap / north-south plane (routable) for NCCL + Open MPI wireup.
  export NCCL_SOCKET_IFNAME="${NCCL_SOCKET_IFNAME:-${NVIS_FAB_NS_IFNAME}}"
  export OMPI_MCA_btl_tcp_if_include="${OMPI_MCA_btl_tcp_if_include:-${NVIS_FAB_NS_IFNAME}}"
  export OMPI_MCA_oob_tcp_if_include="${OMPI_MCA_oob_tcp_if_include:-${NVIS_FAB_NS_IFNAME}}"
  # UCX transport: forbid the tcp/IPoIB transport (unroutable cross-rack); use RDMA.
  export UCX_TLS="${UCX_TLS:-^tcp}"
  # UCC's cross-rack address exchange segfaults on unroutable IPoIB; drop it (NCCL,
  # not MPI collectives, does the real work in the perf tests).
  export OMPI_MCA_coll_ucc_enable="${OMPI_MCA_coll_ucc_enable:-0}"

  echo "nvis_fabric: link=${NVIS_FAB_LINK} rate=${NVIS_FAB_RATE}G rails=${NVIS_FAB_RAILS}" \
       "NCCL_IB_HCA=${NCCL_IB_HCA:-<auto: per-node detect>} NCCL_SOCKET_IFNAME=${NCCL_SOCKET_IFNAME}" >&2
}

# On-node: scan local sysfs + export.
nvis_fabric_export() {
  _nvis_fabric_apply "$(nvis_fabric_scan)"
}

# Run the scan on a remote node and print its NVIS_FAB_* lines (stdout).
# $1 = node hostname; $2.. = optional ssh command (default: ssh with batch opts).
nvis_fabric_scan_node() {
  local node="$1"; shift || true
  [[ -n "$node" ]] || { echo "nvis_fabric_scan_node: no node given" >&2; return 1; }
  local ssh_cmd="${*:-ssh -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=8}"
  local src; src="${BASH_SOURCE[0]}"
  [[ -f "${src}" ]] || { echo "nvis_fabric: cannot locate fabric.inc.sh to ship (${src})" >&2; return 1; }
  # Build a single remote script = env we want honoured + this include's functions
  # + a call to the scanner. Piped as one stdin stream to `bash -s` on the node
  # (can't use both a file redirect and a heredoc for stdin).
  {
    [[ -n "${NVIS_RDMA_LINK_TECH:-}" ]] && printf 'export NVIS_RDMA_LINK_TECH=%q\n' "${NVIS_RDMA_LINK_TECH}"
    [[ -n "${NVIS_FABRIC_NS_IFNAME:-}" ]] && printf 'export NVIS_FABRIC_NS_IFNAME=%q\n' "${NVIS_FABRIC_NS_IFNAME}"
    cat "${src}"
    printf '\nnvis_fabric_scan\n'
  } | ${ssh_cmd} "${node}" "bash -s" 2>/dev/null
}

# Head/submit-time: run the scan on a representative compute node + export here.
# Assumes homogeneous rack nodes (same NIC layout), which holds within a rack.
nvis_fabric_export_from_node() {
  local node="$1"; shift || true
  local remote_scan; remote_scan="$(nvis_fabric_scan_node "${node}" "$@")"
  if [[ -z "${remote_scan}" || "${remote_scan}" != *NVIS_FAB_IB_HCA=* ]]; then
    echo "nvis_fabric: WARNING: remote scan of ${node} returned no usable result; set NCCL_IB_HCA manually or NVIS_FABRIC_SKIP=1" >&2
    return 0
  fi
  _nvis_fabric_apply "${remote_scan}"
}

# Human-readable report (used by the fabric-probe tool). Compares to an expected
# profile when NVIS_FABRIC_EXPECT_RAILS / NVIS_FABRIC_EXPECT_RATE are set.
nvis_fabric_report() {
  local scan; scan="$(nvis_fabric_scan)"
  # shellcheck disable=SC2086
  eval "${scan}"
  echo "fabric: link=${NVIS_FAB_LINK} top_rate=${NVIS_FAB_RATE}G compute_rails=${NVIS_FAB_RAILS}"
  echo "  NCCL_IB_HCA=${NVIS_FAB_IB_HCA}"
  echo "  UCX_NET_DEVICES=${NVIS_FAB_UCX_NET_DEVICES}"
  echo "  NCCL_SOCKET_IFNAME=${NVIS_FAB_NS_IFNAME}"
  local gpus; gpus="$(_nvis_fabric_gpu_count)"
  echo "  gpus_per_node=${gpus}"
  if [[ -n "${NVIS_FABRIC_EXPECT_RAILS:-}" ]]; then
    if [[ "${NVIS_FAB_RAILS}" == "${NVIS_FABRIC_EXPECT_RAILS}" ]]; then
      echo "  OK: rails match expected (${NVIS_FABRIC_EXPECT_RAILS})"
    else
      echo "  DISCREPANCY: expected ${NVIS_FABRIC_EXPECT_RAILS} rails, found ${NVIS_FAB_RAILS}"
    fi
  fi
  if [[ -n "${NVIS_FABRIC_EXPECT_RATE:-}" ]]; then
    if [[ "${NVIS_FAB_RATE}" == "${NVIS_FABRIC_EXPECT_RATE}" ]]; then
      echo "  OK: rate matches expected (${NVIS_FABRIC_EXPECT_RATE}G)"
    else
      echo "  DISCREPANCY: expected ${NVIS_FABRIC_EXPECT_RATE}G rails, found ${NVIS_FAB_RATE}G"
    fi
  fi
}
