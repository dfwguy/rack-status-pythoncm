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
#   1. Consider every RDMA device whose port 1 is state=ACTIVE, except bonds (mlx5_bond_*,
#      the OOB bond0) and the NVSwitch-attached HCA (one PCIe device with 4 functions).
#   2. Split by link_layer: InfiniBand vs Ethernet(RoCE). Pick the technology from the cluster's
#      fabric network (NVIS_FABRIC_NETWORK: IB|RoCE|auto, see nvis_fabric_network; auto = IB if
#      any active IB port exists, else RoCE).
#      A stated technology (the cluster's fabric.scaleout, else its platform's default) is an
#      expectation the node confirms: when the node has no candidate of that technology but does
#      have the other, the node's own wins, with a warning naming the mismatch. A platform
#      default describes a family of sites (gb300-nvl72 says ib; NscaleTX-NC is Spectrum-X RoCE),
#      and honouring it against the hardware left NC with no rails, NCCL on every device and
#      "GID index -1". Where the stated technology is present, nothing changes.
#   3. InfiniBand: keep the ports at the TOP link rate (drops slower BlueField-3 storage
#      NICs). IB GIDs live on fe80::/64 by default, so no GID rule applies there.
#   4. RoCE: keep a port only if it has a ROUTABLE GID (not zero, not fe80::) and its netdev
#      does not carry a default route. Rate is NOT used: on GB300 NVL72 Spectrum-X the compute
#      rails are 200G VFs (roce_vf_r*) while the management NICs are 400G, so a top-rate rule
#      picked exactly the wrong devices. The PFs behind the VFs (roce_r*) are OVS uplinks with
#      no routable GID and drop out by rule 4. The kept GIDs decide the address family: all
#      IPv6 (no IPv4-mapped GID) -> NCCL_IB_ADDR_FAMILY=AF_INET6, else AF_INET. NCCL's GID
#      auto-select needs the family; the GID *index* differs per node, so it is never pinned.
#   5. RoCE only: if any other ACTIVE device exists, NCCL left to itself would use it too, so
#      NCCL_IB_HCA is pinned -- to the rails' common name prefix (e.g. roce_vf_r, which NCCL
#      prefix-matches) when no other device starts with it, since that string is identical on
#      every node even when one node lacks a rail; otherwise to the exact list. IB keeps its
#      per-rank auto-detect (see the job 65648 note in _nvis_fabric_apply).
#   6. Sanity gate: if the count of selected rails is < the GPU count we log a
#      warning (likely mis-detection) but still proceed with what we found.
#
# EVERY exported var is overridable: if the caller already set it (site override,
# platform tool_param_defaults forwarded from the app), we keep their value.
#
# Public functions:
#   nvis_fabric_scan            — print eval-able NVIS_FAB_* assignments (local sysfs)
#   nvis_fabric_export          — scan locally + export NCCL/UCX/OMPI env (on-node use)
#   nvis_fabric_export_pinned   — the same, then pin NCCL/UCX to this node's exact rails (=list)
#   nvis_fabric_export_compute  — ONLY the RoCE compute plane (NCCL_IB_HCA pin, NCCL_IB_ADDR_FAMILY)
#   nvis_fabric_ns_ifname       — print the routable bootstrap netdev (bond0 if present, else routable)
#   nvis_fabric_export_from_node <node> [ssh]  — scan a remote node + export (head/submit-time use)
#   nvis_fabric_report          — human-readable classification (+ expected-vs-actual if NVIS_FABRIC_EXPECT_*)
#   nvis_fabric_container_env   — what a pyxis/enroot container needs on this fabric (RDMA devices,
#                                 the cluster's NCCL net plugin); lists the keys in NVIS_FABRIC_CONTAINER_EXPORTS
#
# Env inputs (the cluster's fabric record projects the first three -- catalog.models.ClusterFabric):
#   NVIS_FABRIC_NETWORK      IB | RoCE | auto   the cluster's fabric protocol (default auto); the
#                            deprecated NVIS_RDMA_LINK_TECH is read only when this is unset
#   NVIS_FABRIC_HCA          NCCL_IB_HCA value naming the compute plane, e.g. roce_vf_r (default: detect)
#   NVIS_FABRIC_ADDR_FAMILY  ipv4 | ipv6        (default: detect from the compute GIDs)
#   NVIS_FABRIC_NET_PLUGIN   NCCL net plugin path inside the job's container, or none (default: NCCL's choice)
#   GPUS_PER_NODE            gpu count for the gate (default: nvidia-smi -L count, else 4)
#   NVIS_FABRIC_NS_IFNAME    bootstrap/NS netdev override (default: bond0 if up, else first up eth)
#   NVIS_FABRIC_EXPECT_RAILS / NVIS_FABRIC_EXPECT_RATE  optional expected profile (validation)
# Test seams: NVIS_FABRIC_SYSFS (ib root), NVIS_FABRIC_NETFS (net root), NVIS_FABRIC_PROCNET,
#   NVIS_FABRIC_DEFAULT_ROUTE_IFS / NVIS_FABRIC_NVSWITCH_BDFS (set, even empty, to skip probing).

# Netdevs carrying a default route (the management plane), one per line.
_nvis_fabric_default_route_ifs() {
  if [[ -n "${NVIS_FABRIC_DEFAULT_ROUTE_IFS+x}" ]]; then
    # shellcheck disable=SC2086
    printf '%s\n' ${NVIS_FABRIC_DEFAULT_ROUTE_IFS}
    return 0
  fi
  # /proc/net when there is no `ip` -- inside a pyxis container, where the NeMo launchers run
  # this scan. Containers share the host network namespace, so the routes are the host's. Without
  # this the exclusion silently did nothing there and a default-route NIC with a routable GID
  # (NscaleTX-NC's mlx5_12) was taken for a rail. NVIS_FABRIC_PROCNET is the test seam.
  local procnet="${NVIS_FABRIC_PROCNET:-/proc/net}"
  if [[ -z "${NVIS_FABRIC_PROCNET:-}" ]] && command -v ip >/dev/null 2>&1; then
    { ip -o route show default; ip -6 -o route show default; } 2>/dev/null \
      | awk '{for (i = 1; i < NF; i++) if ($i == "dev") print $(i + 1)}' | sort -u
    return 0
  fi
  {
    # route: Iface Destination ... -- a default route is Destination 00000000 (hex, little-endian).
    [[ -r "${procnet}/route" ]] && awk 'NR > 1 && $2 == "00000000" {print $1}' "${procnet}/route"
    # ipv6_route: dest dest_len ... iface -- a default route is ::/0.
    [[ -r "${procnet}/ipv6_route" ]] \
      && awk '$1 == "00000000000000000000000000000000" && $2 == "00" && $NF != "lo" {print $NF}' "${procnet}/ipv6_route"
  } | sort -u
}

# PCI bus addresses (BDF minus function) of the NVSwitch-attached HCA: one device, 4 functions.
_nvis_fabric_nvswitch_bdfs() {
  if [[ -n "${NVIS_FABRIC_NVSWITCH_BDFS+x}" ]]; then
    # shellcheck disable=SC2086
    printf '%s\n' ${NVIS_FABRIC_NVSWITCH_BDFS}
    return 0
  fi
  command -v lspci >/dev/null 2>&1 || return 0
  lspci -D 2>/dev/null | grep -i infiniband | awk '{print $1}' | cut -d. -f1 \
    | sort | uniq -c | awk '$1 == 4 {print $2}'
}

# Routable-GID family of one port dir: v6, v4, both, or nothing when it has no routable GID.
# Zero and link-local (fe80::) GIDs are skipped; ::ffff:a.b.c.d is an IPv4-mapped GID.
_nvis_fabric_gid_family() {
  local g v4=0 v6=0
  for g in "$1"/gids/*; do
    [[ -r "$g" ]] || continue
    g="$(cat "$g" 2>/dev/null)"
    [[ -z "$g" || "$g" == 0000:0000:0000:0000:0000:0000:0000:0000 || "$g" == fe80:* ]] && continue
    if [[ "$g" == 0000:0000:0000:0000:0000:ffff:* ]]; then v4=1; else v6=1; fi
  done
  if (( v4 && v6 )); then printf 'both'; elif (( v6 )); then printf 'v6'; elif (( v4 )); then printf 'v4'; fi
}

# Longest common prefix of the arguments.
_nvis_fabric_common_prefix() {
  local p="$1" s
  shift
  for s in "$@"; do
    while [[ -n "$p" && "$s" != "$p"* ]]; do p="${p%?}"; done
  done
  printf '%s' "$p"
}

# --- local sysfs scan -------------------------------------------------------
# Prints (to stdout) eval-able assignments:
#   NVIS_FAB_LINK=ib|roce  NVIS_FAB_RATE=<gbps>  NVIS_FAB_RAILS=<n>
#   NVIS_FAB_IB_HCA=roce_vf_r1,...      exact rail list (empty = no compute rails found)
#   NVIS_FAB_UCX_NET_DEVICES=roce_vf_r1:1,...
#   NVIS_FAB_HCA_PIN=roce_vf_r         NCCL_IB_HCA to pin, or empty to leave NCCL to auto-detect
#   NVIS_FAB_ADDR_FAMILY=ipv6|ipv4|    RoCE address family, empty when not determinable / IB
#   NVIS_FAB_NS_IFNAME=<netdev>
# The fabric protocol the cluster declares, raw: NVIS_FABRIC_NETWORK (IB | RoCE), projected from the
# cluster record's fabric.scaleout, else its platform's, with an operator's Settings value between
# the two. NVIS_RDMA_LINK_TECH is the deprecated name, read only when the new one is unset; "auto"
# when nothing is declared. Every script decides how to find the compute interfaces from this, so
# it is defined here, in the one file nvis_fabric_scan_node ships to a node.
nvis_fabric_network() {
  printf '%s' "${NVIS_FABRIC_NETWORK:-${NVIS_RDMA_LINK_TECH:-auto}}"
}

nvis_fabric_scan() {
  local want
  want="$(nvis_fabric_network | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
  [[ "${want}" == "mlx" || "${want}" == "infiniband" ]] && want=ib
  [[ "${want}" == "spectrumx" || "${want}" == "spx" ]] && want=roce

  local ibroot="${NVIS_FABRIC_SYSFS:-/sys/class/infiniband}"
  local mgmt_ifs nvsw_bdfs
  mgmt_ifs=" $(_nvis_fabric_default_route_ifs | tr '\n' ' ') "
  nvsw_bdfs=" $(_nvis_fabric_nvswitch_bdfs | tr '\n' ' ') "

  local d dev st ll rnum nd bdf fam top_ib=0
  local -a ib=() roce=() active=() all_devs=()
  shopt -s nullglob
  for d in "${ibroot}"/*; do
    [[ -d "$d" ]] || continue
    dev="${d##*/}"
    all_devs+=("$dev")
    st="$(cat "$d/ports/1/state" 2>/dev/null)"
    [[ "$st" == *ACTIVE* ]] || continue
    # Every ACTIVE device counts here, bonds included: it is what NCCL could pick unpinned.
    active+=("$dev")
    [[ "$dev" == *bond* ]] && continue
    bdf="$(basename "$(dirname "$(dirname "$(readlink -f "$d")")")")"
    [[ "$nvsw_bdfs" == *" ${bdf%.*} "* ]] && continue
    ll="$(cat "$d/ports/1/link_layer" 2>/dev/null)"
    rnum="$(grep -oE '[0-9]+' "$d/ports/1/rate" 2>/dev/null | head -1)"; rnum="${rnum:-0}"
    case "$ll" in
      InfiniBand)
        ib+=("${dev}:${rnum}")
        (( rnum > top_ib )) && top_ib=$rnum
        ;;
      Ethernet)
        nd="$(ls "$d/device/net" 2>/dev/null | head -1)"
        [[ -n "$nd" && "$mgmt_ifs" == *" $nd "* ]] && continue
        fam="$(_nvis_fabric_gid_family "$d/ports/1")"
        [[ -n "$fam" ]] || continue
        roce+=("${dev}:${rnum}:${fam}")
        ;;
    esac
  done
  shopt -u nullglob

  local link
  case "$want" in
    ib)   link=ib ;;
    roce) link=roce ;;
    *)    if [[ "${#ib[@]}" -gt 0 ]]; then link=ib; else link=roce; fi ;;
  esac
  # The stated technology names what the site should have; the node says what it does have.
  local -i n_ib="${#ib[@]}" n_roce="${#roce[@]}"
  if [[ "$link" == ib && $n_ib -eq 0 && $n_roce -gt 0 ]] || [[ "$link" == roce && $n_roce -eq 0 && $n_ib -gt 0 ]]; then
    local found=ib; [[ "$link" == ib ]] && found=roce
    echo "nvis_fabric: WARNING: fabric network ${want} but this node has no ${link} compute port;" \
         "using the ${found} rails it does have -- correct the cluster's fabric.scaleout" >&2
    link=$found
  fi

  local -a rails=()
  local e name r f top=0 all_v4=1 all_v6=1
  if [[ "$link" == "ib" ]]; then
    top=$top_ib
    for e in "${ib[@]}"; do
      name="${e%%:*}"; r="${e##*:}"
      [[ "$r" == "$top" ]] && rails+=("$name")
    done
  else
    for e in "${roce[@]}"; do
      name="${e%%:*}"; r="${e#*:}"; f="${r#*:}"; r="${r%%:*}"
      rails+=("$name")
      (( r > top )) && top=$r
      [[ "$f" == v4 || "$f" == both ]] || all_v4=0
      [[ "$f" == v6 || "$f" == both ]] || all_v6=0
    done
  fi
  if [[ "${#rails[@]}" -gt 0 ]]; then
    mapfile -t rails < <(printf '%s\n' "${rails[@]}" | sort -V)
  fi

  # Address family (RoCE): every rail reachable over IPv4 keeps NCCL's default; otherwise a
  # family every rail has. A rail set with neither in common gets nothing (logged by apply).
  local addr_family=""
  if [[ "$link" == "roce" && "${#rails[@]}" -gt 0 ]]; then
    if (( all_v4 )); then addr_family=ipv4; elif (( all_v6 )); then addr_family=ipv6; fi
  fi
  case "$(printf '%s' "${NVIS_FABRIC_ADDR_FAMILY:-}" | tr '[:upper:]' '[:lower:]')" in
    ipv4|inet|af_inet)    addr_family=ipv4 ;;
    ipv6|inet6|af_inet6)  addr_family=ipv6 ;;
  esac

  # Pin (RoCE): only when NCCL, unpinned, could reach a device that is not a rail.
  local pin="" a
  local -a others=()
  if [[ "$link" == "roce" && "${#rails[@]}" -gt 0 ]]; then
    for a in "${active[@]}"; do
      [[ " ${rails[*]} " == *" $a "* ]] || others+=("$a")
    done
    if [[ "${#others[@]}" -gt 0 ]]; then
      pin="$(_nvis_fabric_common_prefix "${rails[@]}")"
      for a in "${all_devs[@]}"; do
        [[ " ${rails[*]} " == *" $a "* ]] && continue
        [[ -n "$pin" && "$a" == "$pin"* ]] && { pin=""; break; }
      done
      [[ -n "$pin" ]] || pin="$(IFS=,; echo "${rails[*]}")"
    fi
  fi
  # The cluster's stated compute plane wins over anything detected.
  [[ -n "${NVIS_FABRIC_HCA:-}" ]] && pin="${NVIS_FABRIC_HCA}"

  local hca ucx=""
  hca="$(IFS=,; echo "${rails[*]}")"
  [[ "${#rails[@]}" -gt 0 ]] && ucx="$(printf '%s\n' "${rails[@]}" | sed 's/$/:1/' | paste -sd, -)"

  printf 'NVIS_FAB_LINK=%q\n' "$link"
  printf 'NVIS_FAB_RATE=%q\n' "$top"
  printf 'NVIS_FAB_RAILS=%q\n' "${#rails[@]}"
  printf 'NVIS_FAB_IB_HCA=%q\n' "$hca"
  printf 'NVIS_FAB_UCX_NET_DEVICES=%q\n' "$ucx"
  printf 'NVIS_FAB_HCA_PIN=%q\n' "$pin"
  printf 'NVIS_FAB_ADDR_FAMILY=%q\n' "$addr_family"
  printf 'NVIS_FAB_NS_IFNAME=%q\n' "$(_nvis_fabric_ns_ifname)"
  printf 'NVIS_FAB_GPUS=%q\n' "$(_nvis_fabric_gpu_count)"
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
  # First "up" netdev that isn't loopback/docker/ib/veth/bridge AND carries a routable
  # (global-scope) IPv4. Requiring the IPv4 is the fix for GB300 nodes with no bond0: the old
  # name-only pick grabbed the first "up" device, which was a RoCE-rail bridge (br-rail*) that is
  # "up" with only a link-local IPv6 and no routable address -- Open MPI then aborted the whole
  # run with "None of the TCP networks ... could be found: br-rail1" before NCCL ever started.
  # `ip` is present on every compute node; if it somehow is not, fall back to the name-only pick.
  local n have_ip=0
  command -v ip >/dev/null 2>&1 && have_ip=1
  for n in "${netroot}"/*; do
    nic="$(basename "$n")"
    case "$nic" in lo|docker*|veth*|ib*|virbr*|br-*) continue ;; esac
    [[ -r "$n/operstate" ]] || continue
    [[ "$(tr -d '\n' < "$n/operstate")" == "up" ]] || continue
    if (( have_ip )); then
      ip -o -4 addr show dev "$nic" scope global 2>/dev/null | grep -q . || continue
    fi
    printf '%s' "$nic"; return
  done
  printf 'bond0'  # last-resort default
}

# Public: the routable bootstrap / north-south netdev, for launchers that set their own socket and
# UCX env (the MNUberGEMM MOP). bond0/bond1 when present, else the first up netdev with a global
# IPv4; NVIS_FABRIC_NS_IFNAME overrides.
nvis_fabric_ns_ifname() {
  _nvis_fabric_ns_ifname
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

# --- routable GID of one rail -------------------------------------------------
# The GID index a RoCE rail must use to reach another rack: RoCE v2, routable (not zero, not
# fe80::), of the cluster's address family (NVIS_FABRIC_ADDR_FAMILY, else IPv6 before IPv4-mapped).
# UCX's automatic choice takes a link-local RoCE v2 GID on NscaleTX-NC (indexes 1 and 3 there are
# fe80::, the routable one is 5), so every ClusterKit run timed out in MPI's address exchange. The
# index differs per node (3 on some NC nodes, 5 on most), so it is looked up on the node that uses it.
nvis_fabric_routable_gid_index() {  # $1 = RDMA device -> index on stdout, nothing when none
  local port="${NVIS_FABRIC_SYSFS:-/sys/class/infiniband}/$1/ports/1"
  local fam want v4="" v6="" g t i
  fam="$(printf '%s' "${NVIS_FABRIC_ADDR_FAMILY:-}" | tr '[:upper:]' '[:lower:]')"
  shopt -s nullglob
  for g in "$port"/gids/*; do
    i="${g##*/}"
    t="$(cat "$port/gid_attrs/types/$i" 2>/dev/null)"
    [[ "$t" == "RoCE v2" ]] || continue
    g="$(tr -d '\n' < "$g" 2>/dev/null)"
    case "$g" in
      ""|0000:0000:0000:0000:0000:0000:0000:0000|fe80:*) continue ;;
      0000:0000:0000:0000:0000:ffff:*) [[ -n "$v4" ]] || v4="$i" ;;
      *) [[ -n "$v6" ]] || v6="$i" ;;
    esac
  done
  shopt -u nullglob
  case "$fam" in
    ipv4|inet|af_inet) want="$v4" ;;
    ipv6|inet6|af_inet6) want="$v6" ;;
    *) want="${v6:-$v4}" ;;
  esac
  [[ -n "$want" ]] && printf '%s\n' "$want"
  return 0
}

# --- rail -> GPU pairing ------------------------------------------------------
# Which GPU sits behind each compute rail, for tools that drive one rail with one GPU (ClusterKit,
# perftest). On GB300 Spectrum-X a rail reaches full rate only from its own GPU, through the NIC's
# Data Direct DMA function (Mellanox, PCI class 0x0801) that lives in that GPU's PCI domain; from
# any other GPU the traffic crosses Grace's PCIe link and tops out near 460 Gb/s of 800. Nothing
# names the pairing: nvidia-smi topo reports SYS for every NIC/GPU pair, and the Mellanox functions
# carry no PCIe serial. What does tie them is the VPD read-only keyword V3, a per-chip id the rail's
# PF and its Data Direct function share (probed on NscaleTX-NC: r1=GPU1, r2=GPU0, r3=GPU3, r4=GPU2,
# the pairing perftest measured). VPD is root-only, so it is read directly when readable and with
# passwordless sudo otherwise (NVIS_FABRIC_VPD_SUDO=0 forbids that). Where neither works the GPUs on
# the rail's NUMA node are dealt out in order -- a guess, said so on stderr, and wrong on NC.
# NVIS_FABRIC_RAIL_GPUS ("roce_vf_r1:1,roce_vf_r2:0,...") states the pairing outright.
# Test seam: NVIS_FABRIC_PCIFS (default /sys/bus/pci/devices).

# PCI functions of one vendor whose class starts with $2, one BDF per line in bus order.
_nvis_fabric_pci_funcs() {  # $1 = vendor (0x10de), $2 = class prefix (0x0302)
  local pcifs="${NVIS_FABRIC_PCIFS:-/sys/bus/pci/devices}" f
  shopt -s nullglob
  for f in "$pcifs"/*; do
    [[ "$(cat "$f/vendor" 2>/dev/null)" == "$1" ]] || continue
    [[ "$(cat "$f/class" 2>/dev/null)" == "$2"* ]] || continue
    printf '%s\n' "${f##*/}"
  done | sort
  shopt -u nullglob
}

# The VPD V3 chip id of one PCI function, empty when the VPD cannot be read or has none.
_nvis_fabric_chip_id() {  # $1 = BDF
  local vpd="${NVIS_FABRIC_PCIFS:-/sys/bus/pci/devices}/$1/vpd"
  [[ -e "$vpd" ]] || return 0
  {
    if [[ -r "$vpd" ]]; then
      cat "$vpd" 2>/dev/null
    elif [[ "${NVIS_FABRIC_VPD_SUDO:-1}" == 1 ]]; then
      sudo -n cat "$vpd" 2>/dev/null
    fi
  } | python3 -c '
import sys
b = sys.stdin.buffer.read()
i = 0
while i < len(b):
    t = b[i]
    if t == 0x78:  # end tag
        break
    if t & 0x80:  # large resource: tag, len lo, len hi, data
        n = b[i + 1] | (b[i + 2] << 8)
        data = b[i + 3:i + 3 + n]
        if t == 0x90:  # VPD-R: keyword(2) len(1) value
            j = 0
            while j + 3 <= len(data):
                kw, ln = data[j:j + 2], data[j + 2]
                if kw == b"V3":
                    print(data[j + 3:j + 3 + ln].decode("ascii", "replace").strip("\x00 "))
                    sys.exit(0)
                j += 3 + ln
        i += 3 + n
    else:  # small resource
        i += 1 + (t & 7)
' 2>/dev/null
}

# CPU list ("0-3,8-11") expanded one per line.
_nvis_fabric_cpus() {
  local part
  for part in ${1//,/ }; do
    if [[ "$part" == *-* ]]; then seq "${part%-*}" "${part#*-}"; else printf '%s\n' "$part"; fi
  done
}

# Keep the rails an NCCL_IB_HCA value names (the cluster's fabric.compute_hca): "=" exact names,
# "^" excludes, otherwise name prefixes; ":port" suffixes ignored. Empty spec keeps every rail.
_nvis_fabric_rails_matching() {  # $1 = spec, $2.. = rails
  local spec="$1"; shift
  [[ -n "$spec" ]] || { printf '%s\n' "$@"; return; }
  local mode=prefix
  case "$spec" in =*) mode=exact; spec="${spec#=}" ;; ^*) mode=exclude; spec="${spec#^}" ;; esac
  local rail p hit
  for rail in "$@"; do
    hit=0
    for p in ${spec//,/ }; do
      p="${p%%:*}"
      if [[ "$mode" == exact ]]; then [[ "$rail" == "$p" ]] && hit=1
      else [[ "$rail" == "$p"* ]] && hit=1
      fi
    done
    if [[ "$mode" == exclude ]]; then (( hit )) || printf '%s\n' "$rail"
    else (( hit )) && printf '%s\n' "$rail"
    fi
  done
}

# One line per rail: "<rail> <gpu_index> <numa> <core> <how>" where how = stated | vpd | numa-guess.
# gpu_index is the CUDA index under CUDA_DEVICE_ORDER=PCI_BUS_ID (GPUs in PCI bus order). core is a
# distinct CPU local to the rail (the (k+1)-th of its local_cpulist for the k-th rail on that NUMA
# node, so CPU 0 is never used). Rails default to this node's scan, filtered by NVIS_FABRIC_HCA.
nvis_fabric_rail_map() {
  local ibroot="${NVIS_FABRIC_SYSFS:-/sys/class/infiniband}"
  local pcifs="${NVIS_FABRIC_PCIFS:-/sys/bus/pci/devices}"
  local -a rails=("$@")
  if [[ "${#rails[@]}" -eq 0 ]]; then
    local NVIS_FAB_IB_HCA=""
    eval "$(nvis_fabric_scan 2>/dev/null | grep '^NVIS_FAB_IB_HCA=')"
    [[ -n "$NVIS_FAB_IB_HCA" ]] || return 1
    mapfile -t rails < <(_nvis_fabric_rails_matching "${NVIS_FABRIC_HCA:-}" ${NVIS_FAB_IB_HCA//,/ })
    [[ "${#rails[@]}" -gt 0 ]] || return 1
  fi

  local -a gpus dds
  mapfile -t gpus < <(_nvis_fabric_pci_funcs 0x10de 0x0302)
  [[ "${#gpus[@]}" -gt 0 ]] || mapfile -t gpus < <(_nvis_fabric_pci_funcs 0x10de 0x0300)
  mapfile -t dds < <(_nvis_fabric_pci_funcs 0x15b3 0x0801)
  local -A dd_chip=() used=() per_numa=()
  local dd g
  for dd in "${dds[@]}"; do dd_chip["$dd"]="$(_nvis_fabric_chip_id "$dd")"; done

  local rail dev pf numa cpulist chip gpu how i k core
  for rail in "${rails[@]}"; do
    dev="$(readlink -f "$ibroot/$rail/device" 2>/dev/null)"
    pf="$dev"
    [[ -e "$dev/physfn" ]] && pf="$(readlink -f "$dev/physfn")"
    numa="$(cat "$dev/numa_node" 2>/dev/null)"; [[ "$numa" =~ ^[0-9]+$ ]] || numa=0
    cpulist="$(cat "$dev/local_cpulist" 2>/dev/null)"
    gpu=""; how=""

    local stated="${NVIS_FABRIC_RAIL_GPUS:-}" pair
    for pair in ${stated//,/ }; do
      [[ "${pair%%:*}" == "$rail" ]] && { gpu="${pair##*:}"; how=stated; }
    done

    if [[ -z "$gpu" ]]; then
      chip="$(_nvis_fabric_chip_id "${pf##*/}")"
      if [[ -n "$chip" ]]; then
        for dd in "${dds[@]}"; do
          [[ "${dd_chip[$dd]}" == "$chip" ]] || continue
          for i in "${!gpus[@]}"; do
            g="${gpus[$i]}"
            [[ "${g%%:*}" == "${dd%%:*}" ]] && { gpu="$i"; how=vpd; break; }
          done
          [[ -n "$gpu" ]] && break
        done
      fi
    fi

    if [[ -z "$gpu" ]]; then
      for i in "${!gpus[@]}"; do
        [[ -n "${used[$i]:-}" ]] && continue
        [[ "$(cat "$pcifs/${gpus[$i]}/numa_node" 2>/dev/null)" == "$numa" ]] || continue
        gpu="$i"; how="numa-guess"; break
      done
      echo "nvis_fabric: WARNING: no Data Direct pairing found for ${rail} (VPD unreadable?);" \
           "guessing GPU ${gpu:-none} by NUMA; set NVIS_FABRIC_RAIL_GPUS to state it" >&2
    fi
    [[ -n "$gpu" ]] && used["$gpu"]=1

    k="${per_numa[$numa]:-0}"
    per_numa["$numa"]=$((k + 1))
    core="$(_nvis_fabric_cpus "${cpulist:-0}" | sed -n "$((k + 2))p")"
    [[ -n "$core" ]] || core="$(_nvis_fabric_cpus "${cpulist:-0}" | head -1)"
    printf '%s %s %s %s %s\n' "$rail" "${gpu:--}" "$numa" "${core:-0}" "${how:-none}"
  done
}

# --- export NCCL / UCX / OMPI env from a scan (respects existing overrides) --
# $1..: optional pre-captured NVIS_FAB_* eval lines; if empty, scans locally.
# The compute-plane half of apply, shared with nvis_fabric_export_compute. Reads NVIS_FAB_* from a
# scan already eval'd into the caller. Touches nothing but NCCL_IB_ADDR_FAMILY and NCCL_IB_HCA, and
# on InfiniBand (no pin, no family) exports nothing at all.
_nvis_fabric_apply_compute() {
  # RoCE address family. NCCL defaults to AF_INET; on an IPv6-only RoCE fabric that finds no
  # GID ("local GID index -1") and the transport never comes up.
  case "${NVIS_FAB_ADDR_FAMILY:-}" in
    ipv6) export NCCL_IB_ADDR_FAMILY="${NCCL_IB_ADDR_FAMILY:-AF_INET6}" ;;
    ipv4) export NCCL_IB_ADDR_FAMILY="${NCCL_IB_ADDR_FAMILY:-AF_INET}" ;;
  esac
  # A RoCE compute plane NCCL could not tell apart from the management NICs unaided, or one the
  # cluster states outright (NVIS_FABRIC_HCA).
  if [[ -n "${NVIS_FAB_HCA_PIN:-}" && "${NVIS_FABRIC_PIN_DEVICES:-0}" != "1" ]]; then
    export NCCL_IB_HCA="${NCCL_IB_HCA:-${NVIS_FAB_HCA_PIN}}"
  fi
  return 0
}

_nvis_fabric_apply() {
  local scan="$1"
  # shellcheck disable=SC2086
  eval "${scan}"

  # Bootstrap / north-south plane (routable) for NCCL + Open MPI wireup. Exported FIRST and
  # unconditionally: it is a different plane from the compute rails, and the scan resolves it
  # whether or not any rail is found. Gating it behind the rail check below meant a node whose
  # rail probe came back empty exported nothing, and nccl_allreduce.sbatch fell through to a
  # literal bond0 -- which GB300 nodes do not have -- so mpirun aborted on "None of the TCP
  # networks ... could be found: bond0" even for a single-node NVLink run that needs no rails.
  export NCCL_SOCKET_IFNAME="${NCCL_SOCKET_IFNAME:-${NVIS_FAB_NS_IFNAME}}"
  export OMPI_MCA_btl_tcp_if_include="${OMPI_MCA_btl_tcp_if_include:-${NVIS_FAB_NS_IFNAME}}"
  export OMPI_MCA_oob_tcp_if_include="${OMPI_MCA_oob_tcp_if_include:-${NVIS_FAB_NS_IFNAME}}"
  # Transport-safety defaults, likewise independent of which rails exist. Left behind the rail
  # gate, a no-rails node ran with UCC enabled and every rank died at once with
  # "UCC ERROR failed to create tl context for ucp" then SIGSEGV (rc=139).
  # UCX transport: forbid the tcp/IPoIB transport (unroutable cross-rack); use RDMA.
  export UCX_TLS="${UCX_TLS:-^tcp}"
  # UCC's cross-rack address exchange segfaults on unroutable IPoIB; drop it (NCCL,
  # not MPI collectives, does the real work in the perf tests).
  export OMPI_MCA_coll_ucc_enable="${OMPI_MCA_coll_ucc_enable:-0}"
  # HCOLL, likewise: `--mca pml ob1 --mca btl tcp,self` does NOT constrain MPI collectives, and
  # HPC-X's HCOLL opens its own UCX endpoints on EVERY device at MPI_Init -- including NICs that
  # are not the compute plane. On NscaleTX-NC that is mlx5_13 (link-local GID only): every rank of
  # a cross-rack run stalled in "ibv_create_ah(... dgid=fe80::...) for RC DEVX QP connect on
  # mlx5_13 failed: Connection timed out" before NCCL measured anything. The fix the
  # multirack-nccl-hcoll-nvls rule prescribes; five launchers already set it by hand.
  export OMPI_MCA_coll_hcoll_enable="${OMPI_MCA_coll_hcoll_enable:-0}"

  # Compute-plane selection (RoCE pin + address family). Before the rail gate, so a value the
  # cluster states still applies when this node's probe found no rails.
  _nvis_fabric_apply_compute

  local gpus; gpus="$(_nvis_fabric_gpu_count)"
  if [[ -n "${NVIS_FAB_RAILS:-}" && "${NVIS_FAB_RAILS}" -gt 0 && "${NVIS_FAB_RAILS}" -lt "${gpus}" ]]; then
    echo "nvis_fabric: WARNING: only ${NVIS_FAB_RAILS} compute rail(s) found for ${gpus} GPUs" \
         "(link=${NVIS_FAB_LINK} rate=${NVIS_FAB_RATE}G) — check the cluster's fabric network / cabling" >&2
  fi
  if [[ -z "${NVIS_FAB_IB_HCA:-}" ]]; then
    echo "nvis_fabric: WARNING: no compute rails detected; device selection left to per-rank auto-detect" \
         "(bootstrap NCCL_SOCKET_IFNAME=${NCCL_SOCKET_IFNAME})" >&2
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
  #
  # RoCE is the exception, handled above: there an unpinned NCCL also opens the management
  # NIC, so the scan pins NVIS_FAB_HCA_PIN -- preferably a name prefix, which is the same
  # string on every node and so does not reintroduce the job-65648 failure.
  if [[ "${NVIS_FABRIC_PIN_DEVICES:-0}" == "1" ]]; then
    export NCCL_IB_HCA="${NCCL_IB_HCA:-${NVIS_FAB_IB_HCA}}"
    export UCX_NET_DEVICES="${UCX_NET_DEVICES:-${NVIS_FAB_UCX_NET_DEVICES}}"
  else
    [[ -n "${NCCL_IB_HCA:-}" ]] && export NCCL_IB_HCA
    [[ -n "${UCX_NET_DEVICES:-}" ]] && export UCX_NET_DEVICES
  fi
  echo "nvis_fabric: link=${NVIS_FAB_LINK} rate=${NVIS_FAB_RATE}G rails=${NVIS_FAB_RAILS}" \
       "NCCL_IB_HCA=${NCCL_IB_HCA:-<auto: per-node detect>}" \
       "NCCL_IB_ADDR_FAMILY=${NCCL_IB_ADDR_FAMILY:-<default>} NCCL_SOCKET_IFNAME=${NCCL_SOCKET_IFNAME}" >&2
}

# On-node: scan local sysfs + export.
nvis_fabric_export() {
  _nvis_fabric_apply "$(nvis_fabric_scan)"
}

# On-node, compute plane ONLY: the RoCE NCCL_IB_HCA pin and NCCL_IB_ADDR_FAMILY -- nothing about the
# bootstrap NIC, MPI transports or HCOLL/UCC. For a launcher whose MPI and bootstrap env is already
# validated and must not move (the Llama 405B profile). On InfiniBand it exports nothing.
nvis_fabric_export_compute() {
  local scan
  scan="$(nvis_fabric_scan)"
  # shellcheck disable=SC2086
  eval "${scan}"
  _nvis_fabric_apply_compute
}

# On-node, for launchers that pin by hand: everything nvis_fabric_export does, then pin NCCL and
# UCX to THIS node's exact compute rails -- NCCL_IB_HCA="=r1,r2,..." (the `=` makes it an exact
# match, so mlx5_1 cannot also prefix-match mlx5_10..13 on a B300) and UCX_NET_DEVICES="r1:1,...".
# A caller's own NCCL_IB_HCA / UCX_NET_DEVICES, or the cluster's NVIS_FABRIC_HCA, still win.
# Returns 1 and pins nothing when no rail was found, so the caller decides; it never falls back to
# a guessed mlx5_N list (which is what every caller used to do).
# MPI's own transport in a job that spans racks. NCCL carries the data on the compute rails; MPI only
# bootstraps and moves small control traffic. On InfiniBand, RC on the rails is what the launchers
# have always used and it works: RC addresses peers by LID and needs no routed GID. On RoCE the rails
# sit in routed per-node subnets (Nscale NC: one VRF per rail, a /64 per node), and UCX, which has no
# address-family selector, picks a link-local GID that cannot leave the node's subnet -- NeMo
# multirack job 165605 died in MPI_Allgather on "ibv_create_ah(... dgid=fe80::...) ... roce_vf_r1
# failed: Connection timed out". There MPI goes over TCP on the routable bootstrap netdev, as the
# single-rack NeMo launch and the host-mpirun NCCL launch already do on that cluster. Reads
# NVIS_FAB_LINK / NVIS_FAB_NS_IFNAME from a scan eval'd into the caller (nvis_fabric_export*).
nvis_fabric_mpi_xrack_transport() {
  if [[ "${NVIS_FAB_LINK:-}" == "roce" ]]; then
    export UCX_TLS=tcp
    export UCX_NET_DEVICES="${NVIS_FAB_NS_IFNAME:-$(_nvis_fabric_ns_ifname)}"
  else
    export UCX_TLS=rc
  fi
  echo "nvis_fabric: cross-rack MPI over UCX_TLS=${UCX_TLS} UCX_NET_DEVICES=${UCX_NET_DEVICES:-<auto>} (link=${NVIS_FAB_LINK:-?})" >&2
}

# NCCL_NVLS_ENABLE for a job that spans NVLink domains: prints 1, or 0 when this node's GPU reports
# the unset fabric CliqueId. NCCL groups NVLS multicast teams by CliqueId; when every domain reports
# the sentinel 32766 (0x7ffe) while each ClusterUUID differs, NCCL tries to bind one team across
# separate NVSwitch fabrics and hangs silently in ncclCommInitRank -- see
# .cursor/rules/multirack-nccl-hcoll-nvls.mdc (B). Nscale NC reported 32766 on every rack checked
# (2026-10-11). The site's fix is a unique CliqueId per domain; until then NVLS stays off across
# racks and on within one. A caller's explicit setting should win over this default.
nvis_fabric_xrack_nvls() {
  local clique
  clique="$(nvidia-smi -q -i 0 2>/dev/null | awk -F: '/CliqueId/ { gsub(/[[:space:]]/, "", $2); print $2; exit }')"
  if [[ "${clique}" == "32766" ]]; then
    echo "nvis_fabric: WARNING: GPU fabric CliqueId is unset (32766) on $(hostname); NVLS off for this" \
         "cross-rack job -- give each NVLink domain a unique CliqueId to restore it" >&2
    printf 0
  else
    printf 1
  fi
}

# What a pyxis/enroot container needs to reach the compute fabric, for launchers whose ranks run in
# a container (the LLMB / Comms-Perf-Suite NCCL sweep). Reads NVIS_FAB_LINK from a scan eval'd into
# the caller (nvis_fabric_export*), else the cluster's declared protocol (nvis_fabric_network).
#
#   IB or RoCE   MELLANOX_VISIBLE_DEVICES=all. The node's enroot hook 99-mellanox.sh exposes the
#                RDMA devices only when it is set; without it the container has none, NCCL reports
#                "NET/IB : Using [RO]" (no device) and quietly runs on NET/Socket over the bootstrap
#                netdev -- a 32-node U22+U23 sweep on Nscale NC (Slurm 189003, 2026-10-12) peaked at
#                7 GB/s. Every other container launcher here sets it; this path never did, and a
#                single-rack run never noticed because its traffic stays on NVLink.
#   any fabric   NCCL_NET_PLUGIN=<NVIS_FABRIC_NET_PLUGIN> when the cluster names one (its
#                fabric.net_plugin): a path INSIDE the image, e.g. the Spectrum-X plugin HPC-X ships
#                at /opt/hpcx/nccl_spectrum-x_plugin/lib/libnccl-net-spcx.so. A host path does not
#                exist in the container ("NET/Plugin: Could not find", NeMo job 188619). Blank leaves
#                NCCL's own choice; "none" asks for no external plugin (built-in IB/RoCE transport).
#
# A value the caller already set wins. Exports NVIS_FABRIC_CONTAINER_EXPORTS, the keys set here, for
# the launcher to force into the job's --export.
nvis_fabric_container_env() {
  local link="${NVIS_FAB_LINK:-}"
  if [[ -z "${link}" ]]; then
    case "$(nvis_fabric_network | tr '[:upper:]' '[:lower:]')" in
      ib|infiniband|mlx) link=ib ;;
      roce|spectrumx|spx) link=roce ;;
    esac
  fi
  NVIS_FABRIC_CONTAINER_EXPORTS=""
  if [[ "${link}" == "ib" || "${link}" == "roce" ]]; then
    export MELLANOX_VISIBLE_DEVICES="${MELLANOX_VISIBLE_DEVICES:-all}"
    NVIS_FABRIC_CONTAINER_EXPORTS="MELLANOX_VISIBLE_DEVICES"
  fi
  if [[ -n "${NVIS_FABRIC_NET_PLUGIN:-}" ]]; then
    export NCCL_NET_PLUGIN="${NCCL_NET_PLUGIN:-${NVIS_FABRIC_NET_PLUGIN}}"
    NVIS_FABRIC_CONTAINER_EXPORTS="${NVIS_FABRIC_CONTAINER_EXPORTS:+${NVIS_FABRIC_CONTAINER_EXPORTS} }NCCL_NET_PLUGIN"
  fi
  export NVIS_FABRIC_CONTAINER_EXPORTS
  echo "nvis_fabric: container wiring link=${link:-unknown}" \
       "MELLANOX_VISIBLE_DEVICES=${MELLANOX_VISIBLE_DEVICES:-<unset: no RDMA devices in the container>}" \
       "NCCL_NET_PLUGIN=${NCCL_NET_PLUGIN:-<NCCL default>}" >&2
  return 0
}

nvis_fabric_export_pinned() {
  local had_hca="${NCCL_IB_HCA:-}" had_ucx="${UCX_NET_DEVICES:-}"
  _nvis_fabric_apply "$(nvis_fabric_scan)"
  if [[ -z "${NVIS_FAB_IB_HCA:-}" ]]; then
    echo "nvis_fabric: WARNING: no compute rails to pin; leaving NCCL_IB_HCA/UCX_NET_DEVICES to the caller" >&2
    return 1
  fi
  if [[ -n "$had_hca" ]]; then
    export NCCL_IB_HCA="$had_hca"
  elif [[ -n "${NVIS_FABRIC_HCA:-}" ]]; then
    export NCCL_IB_HCA="${NVIS_FABRIC_HCA}"
  else
    export NCCL_IB_HCA="=${NVIS_FAB_IB_HCA}"
  fi
  export UCX_NET_DEVICES="${had_ucx:-${NVIS_FAB_UCX_NET_DEVICES}}"
  return 0
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
    [[ -n "${NVIS_FABRIC_NETWORK:-}" ]] && printf 'export NVIS_FABRIC_NETWORK=%q\n' "${NVIS_FABRIC_NETWORK}"
    [[ -n "${NVIS_RDMA_LINK_TECH:-}" ]] && printf 'export NVIS_RDMA_LINK_TECH=%q\n' "${NVIS_RDMA_LINK_TECH}"
    [[ -n "${NVIS_FABRIC_NS_IFNAME:-}" ]] && printf 'export NVIS_FABRIC_NS_IFNAME=%q\n' "${NVIS_FABRIC_NS_IFNAME}"
    [[ -n "${NVIS_FABRIC_HCA:-}" ]] && printf 'export NVIS_FABRIC_HCA=%q\n' "${NVIS_FABRIC_HCA}"
    [[ -n "${NVIS_FABRIC_ADDR_FAMILY:-}" ]] && printf 'export NVIS_FABRIC_ADDR_FAMILY=%q\n' "${NVIS_FABRIC_ADDR_FAMILY}"
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
  echo "  NCCL_IB_HCA (pinned)=${NVIS_FAB_HCA_PIN:-<auto: per-node detect>}"
  echo "  NCCL_IB_ADDR_FAMILY=${NVIS_FAB_ADDR_FAMILY:-<default>}"
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
