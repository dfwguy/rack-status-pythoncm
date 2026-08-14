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
# node_inventory.sh — emit this node's hardware/firmware identity for the validation report.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Runs ON a compute node (fanned out by the report workflow) and prints one TAB-separated
# "key<TAB>value" line per field. The caller parses it into the report's firmware_inventory.
#
# Why key/TAB and not JSON: this runs on thousands of nodes through several SSH hops, and
# hand-rolled JSON quoting in bash breaks on the first serial with a stray character. A flat
# key/value stream cannot produce a malformed document, and the Python side owns the schema.
#
# Repeated entities are indexed: gpu.0.uuid, nic.1.firmware. Missing values are simply absent
# rather than empty — the caller distinguishes "not measured" from "measured as blank", which
# is the whole point of an inventory nobody has to trust blindly.
#
# Must stay cheap: at ~2000 nodes, a second of avoidable work per node is half an hour of sweep.
set -uo pipefail

emit() { [ -n "${2:-}" ] && printf '%s\t%s\n' "$1" "$2"; return 0; }

# sudo only if it is passwordless — a node that would prompt must degrade, never hang the sweep.
S=""
if [ "$(id -u)" -ne 0 ] && sudo -n true 2>/dev/null; then S="sudo -n"; fi

emit host.hostname "$(hostname -s 2>/dev/null)"
emit host.kernel "$($S uname -r 2>/dev/null)"
# shellcheck disable=SC1091
[ -r /etc/os-release ] && emit host.os "$(. /etc/os-release && echo "$PRETTY_NAME")"

# DMI: serial is the vendor service tag, which is the field a hardware RMA is filed against.
# Prefer sysfs (no root, no fork) and fall back to dmidecode only when sysfs is restricted.
sysfs_dmi() { [ -r "/sys/class/dmi/id/$1" ] && tr -d '\n' < "/sys/class/dmi/id/$1"; }
serial="$(sysfs_dmi product_serial)"
[ -z "$serial" ] && serial="$($S dmidecode -s system-serial-number 2>/dev/null | head -1)"
emit host.serial "$serial"
bios="$(sysfs_dmi bios_version)"
[ -z "$bios" ] && bios="$($S dmidecode -s bios-version 2>/dev/null | head -1)"
emit host.bios_version "$bios"
emit host.chassis_model "$(sysfs_dmi product_name)"

# CPU via lscpu, not /proc/cpuinfo: on Grace (aarch64) there is no "model name" and no
# "physical id", so the x86 idiom silently yields an empty model and 0 sockets. lscpu
# normalises both across architectures; /proc/cpuinfo stays as the x86 fallback.
if command -v lscpu >/dev/null 2>&1; then
  _lscpu="$(lscpu 2>/dev/null)"
  emit host.cpu_model "$(awk -F': +' '/^Model name:/{print $2; exit}' <<< "$_lscpu")"
  emit host.cpu_sockets "$(awk -F': +' '/^Socket\(s\):/{print $2; exit}' <<< "$_lscpu")"
  emit host.cpu_cores_per_socket "$(awk -F': +' '/^Core\(s\) per socket:/{print $2; exit}' <<< "$_lscpu")"
else
  emit host.cpu_model "$(awk -F': ' '/^[Mm]odel name/{print $2; exit}' /proc/cpuinfo 2>/dev/null)"
  emit host.cpu_sockets "$(awk -F': ' '/^physical id/{print $2}' /proc/cpuinfo 2>/dev/null | sort -u | wc -l)"
fi
emit host.cpu_cores_total "$(nproc 2>/dev/null)"
# MemTotal is kB and always slightly under the nameplate; round to the nearest 16 GiB so a
# 960 GB node reports 960 rather than 943 and every node in a fleet agrees on one baseline.
emit host.memory_gb "$(awk '/^MemTotal:/{gb=$2/1048576; print int((gb+8)/16)*16}' /proc/meminfo 2>/dev/null)"

# ---- GPUs: one nvidia-smi call supplies every field the schema marks required on a gpuEntry.
if command -v nvidia-smi >/dev/null 2>&1; then
  i=0
  while IFS=, read -r idx uuid serial_g name bdf vbios; do
    [ -z "${idx// /}" ] && continue
    emit "gpu.$i.index" "${idx// /}"
    emit "gpu.$i.uuid" "${uuid# }"
    emit "gpu.$i.serial" "${serial_g# }"
    emit "gpu.$i.model" "${name# }"
    emit "gpu.$i.pcie_bdf" "${bdf# }"
    emit "gpu.$i.vbios_version" "${vbios# }"
    i=$((i + 1))
  done < <(nvidia-smi --query-gpu=index,uuid,serial,name,pci.bus_id,vbios_version \
             --format=csv,noheader 2>/dev/null)
  emit gpu.count "$i"
  emit gpu.driver_version "$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)"
  emit gpu.cuda_runtime "$(nvidia-smi -q 2>/dev/null | awk -F': ' '/CUDA Version/{print $2; exit}' | tr -d ' ')"
fi
command -v nv-fabricmanager >/dev/null 2>&1 &&
  emit gpu.fabric_manager_version "$(nv-fabricmanager --version 2>/dev/null | awk -F': ' '{print $NF; exit}')"
command -v nvidia-imex >/dev/null 2>&1 &&
  emit gpu.imex_version "$(nvidia-imex --version 2>/dev/null | awk -F': ' '{print $NF; exit}')"

# ---- NICs. ibstat's own device order is stable, so index by it rather than by a glob.
if command -v ibstat >/dev/null 2>&1; then
  i=0
  for dev in $(ibstat -l 2>/dev/null); do
    # The compute rails are what the report is about; the management bond is not a fabric NIC.
    case "$dev" in *bond*) continue ;; esac
    emit "nic.$i.device" "$dev"
    emit "nic.$i.port" 1
    emit "nic.$i.firmware" "$(ibstat "$dev" 2>/dev/null | awk -F': ' '/Firmware version/{print $2; exit}')"
    emit "nic.$i.rate_gbps" "$(cat "/sys/class/infiniband/$dev/ports/1/rate" 2>/dev/null | awk '{print $1}')"
    emit "nic.$i.state" "$(awk -F': ' '{print $2}' "/sys/class/infiniband/$dev/ports/1/state" 2>/dev/null)"
    # The IB port GUID is the real identity of an IB port. The report schema has no field for
    # it and asks for a 6-octet MAC instead, so it is carried here and the caller decides.
    emit "nic.$i.port_guid" "$(ibstat "$dev" 2>/dev/null | awk -F': ' '/Port GUID/{print $2; exit}')"
    # InfiniBand vs Ethernet is the RoCE question, and it is a per-port property: a cluster can
    # run IB on the compute rails and Ethernet on the management bond. Read it rather than
    # inferring from device names.
    emit "nic.$i.link_layer" "$(cat "/sys/class/infiniband/$dev/ports/1/link_layer" 2>/dev/null)"
    bdf="$(basename "$(readlink -f "/sys/class/infiniband/$dev/device" 2>/dev/null)" 2>/dev/null)"
    emit "nic.$i.pcie_bdf" "$bdf"
    nd="$(ls "/sys/class/infiniband/$dev/device/net" 2>/dev/null | head -1)"
    emit "nic.$i.netdev" "$nd"
    [ -n "$nd" ] && emit "nic.$i.mac" "$(tr 'a-f' 'A-F' < "/sys/class/net/$nd/address" 2>/dev/null)"
    i=$((i + 1))
  done
  emit nic.count "$i"
fi
emit nic.model "$(lspci -d 15b3: 2>/dev/null | grep -iv bridge | head -1 | sed 's/.*: //')"
command -v ofed_info >/dev/null 2>&1 && emit nic.ofed_version "$(ofed_info -s 2>/dev/null | head -1 | tr -d ':')"

command -v dcgmi >/dev/null 2>&1 &&
  emit software.dcgm_version "$(dcgmi --version 2>/dev/null | awk -F': ' '/version/{print $NF; exit}')"

# ---- BMC. Asked of the node itself rather than of BCM: `cmsh device list -f ip` returns the
# provisioning address, not the controller, and there is no fleet-wide cmsh field for the BMC —
# it lives on a per-device interface, so via cmsh this would be one slow call per node. The node
# already knows its own out-of-band address, and the sweep is already here.
if command -v ipmitool >/dev/null 2>&1; then
  lan="$($S ipmitool lan print 2>/dev/null)"
  # Channel 1 is the common default when the implicit channel returns nothing.
  [ -z "$lan" ] && lan="$($S ipmitool lan print 1 2>/dev/null)"
  # Take everything after the FIRST colon, not field 2 of a colon split — a MAC is itself full
  # of colons and a naive split truncates it to its first octet. Anchoring the match on the
  # colon also keeps "IP Address Source" from matching the "IP Address" line.
  after_colon() { awk -v pat="$1" '$0 ~ pat {sub(/^[^:]*:[[:space:]]*/, ""); print; exit}'; }
  emit bmc.ip "$(after_colon '^IP Address[[:space:]]+:' <<< "$lan")"
  emit bmc.mac "$(after_colon '^MAC Address[[:space:]]+:' <<< "$lan" | tr 'a-f' 'A-F')"
  emit bmc.firmware "$($S ipmitool mc info 2>/dev/null | after_colon '^Firmware Revision')"
fi

exit 0
