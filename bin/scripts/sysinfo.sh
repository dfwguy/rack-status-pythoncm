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
# Author: Junli Zhang<junliz@nvidia.com>
# Author: Rafael M. Koike & Cursor Agent ("Ada")  # node-user + per-node sudo + per-check dispatcher
#
# Node inventory / precheck. Fans out to the compute nodes via pdsh+ssh as the
# invoking (rack-status) user, escalating ONLY the few node-local commands that
# need root (dmidecode/dmesg/mlxlink/ipmitool/ethtool) with `sudo -n` ON EACH NODE
# (a no-op when already root). Do NOT run the whole script under sudo on the head:
# pdsh would then ssh to nodes AS ROOT (usually no node SSH -> exit 255).
#
# pdsh executes its stdin payload LINE BY LINE (each line is a separate remote
# shell), so a privileged check's `sudo` resolution and the command it guards MUST
# stay on the SAME line: `S=; ... && S="sudo -n"; $S <command>`.
#
# ── Per-check dispatcher (composition) ─────────────────────────────────────────
# Usage:
#   sysinfo.sh <nodelist>                 # run ALL checks (display; exit 0) — legacy
#   sysinfo.sh --check <name> <nodelist>  # run ONE check; gating checks exit != 0 on issue
#   sysinfo.sh --list                     # list check ids (id  gate|info  description)
#
# Each check is a function `check_<id>`. "gate" checks return non-zero when their
# "Expected" criterion is violated (used by the workflow so each check is an
# independently pass/failable step). "info" checks only display and always pass.
# Gating is applied only where the failure signal is unambiguous from the check's
# own output; uniformity / platform-count-specific "Expected" notes stay info to
# avoid false failures (promote to gate once detection is validated per platform).
export topdir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="${RACK_SLURM_BIN:-/cm/local/apps/slurm/25.05/bin}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

## Caution: highly depends on pdsh to run commands on all nodes; BCM should be
## configured to let pdsh work without a password.
export PDSH_SSH_ARGS_APPEND="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=quiet -o CheckHostIP=no"
export PDSH_RCMD_TYPE=ssh
export FANOUT=100

# ── Modernized fan-out ─────────────────────────────────────────────────────────
# Shim `pdsh` + `dshbak` onto the dependency-light Python helper (nvis_fanout.py =
# parallel ssh + dshbak-style output grouping). This removes the hard pdsh +
# dshbak(Perl) dependency — neither is installed by default on modern distros —
# while every check body below stays unchanged. The pdsh heredoc becomes the
# helper's stdin; `dshbak` is a passthrough (`cat`) since the helper already groups.
# Set NVIS_FANOUT_LEGACY=1 to force the real pdsh/dshbak if you prefer them.
if [ "${NVIS_FANOUT_LEGACY:-0}" != "1" ] && command -v python3 >/dev/null 2>&1 && [ -f "${topdir}/nvis_fanout.py" ]; then
  pdsh() {
    local nodes=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -w) nodes="$2"; shift 2 ;;
        -w*) nodes="${1#-w}"; shift ;;
        -f|-t|-u|-l|-R) shift 2 ;;   # pdsh flags that take an argument
        *) shift ;;
      esac
    done
    python3 "${topdir}/nvis_fanout.py" --nodes "${nodes}"
  }
  dshbak() { cat; }                  # helper already coalesces; -c is absorbed here
fi

# ── Ordered check registry: "id|gate|description" ──────────────────────────────
CHECKS=(
  "bios|info|Check BIOS"
  "os|info|Check OS"
  "kernel_param|info|Check Kernel Parm"
  "cpu|info|Check CPU"
  "cpu_freq|gate|Check CPU Frequency (performance mode, >1800 MHz)"
  "memory|info|Check Memory"
  "numa_balancing_thp|info|Check NUMA_BALANCING and THP"
  "numa|info|Check NUMA"
  "gpu_basic|info|Check GPU basic"
  "nvlink_status|info|Check NVLINK status"
  "nvlink_ber|gate|Check NVLINK BER (expect none)"
  "p2p_nvlink|info|Check P2P nvlink capability"
  "p2p_pcie|info|Check P2P pcie capability"
  "cuuid|info|Check CUUID"
  "ib_devices|gate|Check IB devices status (all active)"
  "ib_link_down_status|gate|Check IB Link Down status (no issue)"
  "ib_link_down_counters|gate|Check IB Link Down counters (no issue)"
  "ib_pkey|info|Check IB PKEY (uniform)"
  "ib_fw|info|Check IB FW (uniform)"
  "pcie_lnk|info|Check PCIe LnkCap+LnkStat"
  "pcie_acs|info|Check PCIe ACS"
  "pcie_mrrs|info|Check PCIe MRRS"
  "iface|info|Check IFace device status"
  "bond|info|Check Bond interfaces"
  "imex_channel|info|Check IMEX channel devices"
  "imex_profile|info|Check IMEX profile"
  "imex_nodes_config|info|Check IMEX nodes_config"
  "imex_service|info|Check IMEX service status"
  "imex_readiness|info|Check IMEX readiness"
  "imex_connection|info|Check IMEX connection status"
  "enroot|gate|Check Enroot runtime directory (writable)"
  "dmesg_nv_err|gate|Check dmesg NV_ERR (expect none)"
  "dmesg_call_trace|gate|Check dmesg Call trace (expect none)"
  "dmesg_hw_error|gate|Check dmesg Hardware Error (expect none)"
  "dmesg_xid|gate|Check dmesg Xid (expect none)"
  "dmesg_knvlink|gate|Check dmesg knvlinkDiscoverPostRxDetLinks (expect none)"
  "dmesg_ipmi_ssif|gate|Check dmesg ipmi_ssif Not present (expect none)"
  "dmesg_mlx5_health|gate|Check dmesg mlx5_core Health issue (expect none)"
)

# ── Failure detectors (return 0 == issue found) ────────────────────────────────
_detect_nonempty()  { [ -n "$(printf '%s' "$1" | tr -d '[:space:]')" ]; }
_detect_count_gt0() { printf '%s\n' "$1" | awk '/^[[:space:]]*[0-9]+[[:space:]]*$/{if($1+0>0)f=1} END{exit(f?0:1)}'; }

# ── Checks ─────────────────────────────────────────────────────────────────────
check_bios() {
  echo -e "\n########INFO: Check BIOS"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
grep -r . /sys/class/dmi/id/bios_* 2>/dev/null|cut -d'/' -f6-|sort|paste -s -d','
EOF
}

check_os() {
  echo -e "\n########INFO: Check OS"
  pdsh -t 5 -u 5 -f 100 -R ssh -w ${NODELIST} <<< "uname -r; df -Th|grep /cm/node-installer &>/dev/null || echo 'OS is READY'" 2>/dev/null | dshbak -c
}

check_kernel_param() {
  echo -e "\n########INFO: Check Kernel Parm"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
uname -r
cat /proc/cmdline | tr ' ' '\n'|grep -v -E 'ip=|BOOT' | sort | paste -s -d ' '
EOF
}

check_cpu() {
  echo -e "\n########INFO: Check CPU"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
lscpu|awk '/Architecture:/,/L3 cache:/{print $0}'|grep -E -v 'GHz|MHz|BogoMIPS|Flags:|cache:'
EOF
}

check_cpu_freq() {
  echo -e "\n########INFO: Check CPU Frequency(Expected: CPUs are in performance mode and frequency all above 1800 MHz)"
  local out; out="$(pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
if [ -f /sys/devices/system/cpu/cpufreq/policy0/scaling_governor ]; then cat /sys/devices/system/cpu/cpufreq/policy*/scaling_governor|sort|uniq -c; else echo "CPU frequency scaling not supported"; fi
[ $(uname -m) == x86_64 ] && { cpupower monitor -m Mperf|awk '/Mperf/,/^$/{print $0}' |grep -v -e Mperf -e Freq | awk -F '|' '{print $1,$NF}' |sort -n| awk '{key=$1; val=$2; sum[key]+=val;count[key]++} END{for(k in sum){avg=sum[k]/count[k]; status=(avg > 1800 ? "PASS" : "FAIL");printf "CPU Freq: Socket%s %s\n", k, status}}'|paste -s -d','; } || true
[ $(uname -m) == aarch64 ] && { m=; if [ -f /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq ]; then m=cpuinfo_cur_freq; else [ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq ] && m=scaling_cur_freq; fi; [ ! -z ${m} ] && { for i in $(ls -1 /sys/devices/system/node|grep node|sed -e 's:node::g'); do cat /sys/devices/system/node/node${i}/cpu*/cpufreq/$m 2>/dev/null|awk -v node=$i 'BEGIN{sum=0}{sum+=$1}END{if(NR>0){ret=int(sum/NR/1000); if(ret>1800){printf "%s freq PASS\n", node}else{printf "%s freq FAILED\n", node}}}'; done } || echo "CPU frequency info not available"; } || true
EOF
)"
  printf '%s\n' "$out"
  printf '%s' "$out" | grep -qiE 'FAIL(ED)?' && return 1 || return 0
}

check_memory() {
  echo -e "\n########INFO: Check Memory"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
S=; [ "$(id -u)" -ne 0 ] && sudo -n true 2>/dev/null && S="sudo -n"; $S dmidecode -t memory|grep -P '\tVolatile Size:'|tr -d '\t'|sort|uniq -c
EOF
}

check_numa_balancing_thp() {
  echo -e "\n########INFO: Check NUMA_BALANCING and THP(Expected: NUMA_BALANCING=0 and THP=madvise)"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
sysctl kernel.numa_balancing
cat /sys/kernel/mm/transparent_hugepage/enabled
EOF
}

check_numa() {
  echo -e "\n########INFO: Check NUMA"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
numactl -H|grep size:|grep -v ' 0 MB'|awk '{print "N"$2"="int($(NF-1)/1000)"GB"}'|paste -s -d','
EOF
}

check_gpu_basic() {
  echo -e "\n########INFO: Check GPU basic"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
nvidia-smi --format=csv --query-gpu gpu_bus_id,gpu_name,vbios_version,pstate,enforced.power.limit,ecc.errors.uncorrected.aggregate.total,remapped_rows.failure,remapped_rows.uncorrectable,remapped_rows.pending
EOF
}

check_nvlink_status() {
  echo -e "\n#########INFO: Check NVLINK status(Expected: 72 links are up)"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
nvidia-smi nvlink -s|grep '5[0-9].* GB/s'|wc -l
nvidia-smi --format=csv --query-gpu gpu_bus_id,fabric.state,fabric.status
EOF
}

check_nvlink_ber() {
  echo -e "\n#########INFO: Check NVLINK BER(Expected: no output)"
  local out; out="$(pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
nvidia-smi nvlink -e | grep BER | grep -v 15e-255 || true
EOF
)"
  printf '%s\n' "$out"
  _detect_nonempty "$out" && return 1 || return 0
}

check_p2p_nvlink() {
  echo -e "\n#########INFO: Check P2P nvlink capability status(Expected: All GPU are OK on GBXX/BXX/HXX)"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
nvidia-smi topo -p2p n 2>&1|grep GPU[0-9].*OK||true
EOF
}

check_p2p_pcie() {
  echo -e "\n#########INFO: Check P2P pcie capability status(Expected: All GPU are OK on RTX/L20)"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
nvidia-smi topo -p2p p 2>&1|grep GPU[0-9].*OK||true
EOF
}

check_cuuid() {
  echo -e "\n########INFO: Check CUUID(Expected: For GBXX, all nodes in the same rack have the same CUUID)"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
nvidia-smi -q | grep -E 'Bus Id|CliqueId|ClusterUUID'
EOF
}

check_ib_devices() {
  echo -e "\n########INFO: Check IB devices status(Expected: All up and active)"
  local out; out="$(pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
ibstatus|grep -E 'Infiniband|state:'|paste - - -|sort -t'_' -k2n
EOF
)"
  printf '%s\n' "$out"
  # Fail if any state line reports something other than ACTIVE.
  printf '%s\n' "$out" | grep -i 'state:' | grep -qiv 'ACTIVE' && return 1 || return 0
}

check_ib_link_down_status() {
  echo -e "\n########INFO: Check IB Link Down status(Expected: No issue found)"
  #Bypass those 4 ports IB HCA is connected to nvswitch on B200/B300.
  local out; out="$(pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
S=; [ "$(id -u)" -ne 0 ] && sudo -n true 2>/dev/null && S="sudo -n"; ls -l /sys/class/infiniband|grep -v $((lspci -D | grep -i Infiniband|awk '{print $1}'|cut -d '.' -f 1|sort|uniq -c|grep -w 4||echo NONE)|awk '{print $NF}'|xargs -I {} echo "-e {}.")|grep infiniband|awk -F'/' '{print $(NF-2),$NF}'|while read bdf dev; do ret=$($S mlxlink -d $bdf 2>&1|grep Recommendation|cut -f2- -d ':'|grep -v 'No issue was observed'||true); [ "X$ret" != "X" ] && echo "$dev:${ret}"; done|paste -s -d','
EOF
)"
  printf '%s\n' "$out"
  _detect_nonempty "$out" && return 1 || return 0
}

check_ib_link_down_counters() {
  echo -e "\n########INFO: Check IB Link Down counters(Expected: No issue found)"
  #Bypass those 4 ports IB HCA is connected to nvswitch on B200/B300.
  local out; out="$(pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
for dev in $(ls -l /sys/class/infiniband|grep -v $((lspci -D | grep -i Infiniband|awk '{print $1}'|cut -d '.' -f 1|sort|uniq -c|grep -w 4||echo NONE)|awk '{print $NF}'|xargs -I {} echo "-e {}.")|grep infiniband|awk -F'/' '{print $NF}'); do link_downed=$(</sys/class/infiniband/${dev}/ports/1/counters/link_downed); symbol_error=$(</sys/class/infiniband/${dev}/ports/1/counters/symbol_error); if [[ ${link_downed} -ne 0 || ${symbol_error} -ne 0 ]]; then echo ${dev}:link_downed=${link_downed},symbol_error=${symbol_error}; fi; done
EOF
)"
  printf '%s\n' "$out"
  _detect_nonempty "$out" && return 1 || return 0
}

check_ib_pkey() {
  echo -e "\n########INFO: Check IB PKEY(Expected: All nodes have the same PKEY)"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
for i in $(ls -l /sys/class/infiniband|grep -v $((lspci -D | grep -i Infiniband|awk '{print $1}'|cut -d '.' -f 1|sort|uniq -c|grep -w 4||echo NONE)|awk '{print $NF}'|xargs -I {} echo "-e {}.")|grep infiniband|awk -F'/' '{print $NF}'|grep -v bond_|sort -t'_' -k2n); do ret=$(cat /sys/class/infiniband/${i}/ports/1/pkeys/*|grep -E -v '0x0000'|sort|uniq|paste -s -d','); [ ! -z $ret ] && echo "${i}: ${ret}"; done || true
EOF
}

check_ib_fw() {
  echo -e "\n########INFO: Check IB FW(Expected: All nodes have the same FW version)"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
for i in /sys/class/infiniband/*; do printf "%50s%15s\n" ${i}/fw_ver $(<$i/fw_ver); done
EOF
}

check_pcie_lnk() {
  echo -e "\n########INFO: Check PCIe LnkCap+LnkStat(Expected: No issue found, but on BXX/HXX xx:xx.{0..3} shown is expected)"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
[ $(uname -m) == x86_64 ] && { lspci -d 15b3: -nn|awk '{print $1}'|while read i; do [ $(lspci -s $i -vvv|grep -e LnkCap: -e LnkSta:|grep -o Speed.*|awk -F',' '{print $1, $2}'|tr -s ' '|sort|uniq -c|wc -l) -ne 1 ] && echo $i || true; done; } || true
[ $(uname -m) == aarch64 ] && { lspci -d 15b3: -nn|grep controller|awk '{print $1}'|while read i; do [ $(lspci -s $i -vvv|grep -e LnkCap: -e LnkSta:|grep -o Speed.*|awk -F',' '{print $1, $2}'|tr -s ' '|sort|uniq -c|wc -l) -ne 1 ] && echo $i || true; done; } || true
EOF
}

check_pcie_acs() {
  echo -e "\n########INFO: Check PCIe ACS(Expected: All same brand nodes the same)"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
lspci -vvv|grep 'ACSCtl:.*SrcValid-'|wc -l
EOF
}

check_pcie_mrrs() {
  echo -e "\n########INFO: Check PCIe MRRS(Expected: all 4096, but on BXX/HXX some<4> with 512 is expected)"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
lspci -d 15b3: -nn|awk '{print $1}'|xargs -I {} lspci -s {} -vvv|grep -o MaxReadReq.*|awk '{print $(NF-1)}'|sort|uniq -c
EOF
}

check_iface() {
  echo -e "\n########INFO: Check IFace device status(Expected: all nodes use the same interface connect to default gateway)"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
ip r sh|grep default|grep -o dev.*|cut -f2 -d' '
EOF
}

check_bond() {
  echo -e "\n########INFO: Check Bond interfaces(Expected: all nodes the same)"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
S=; [ "$(id -u)" -ne 0 ] && sudo -n true 2>/dev/null && S="sudo -n"; for i in /proc/net/bonding/*; do { $S ethtool ${i##*/} | grep -e Speed -e Link; echo '==DETAILS=='; grep -e 'Bonding Mode' -e 'Transmit Hash Policy' $i; for j in $(grep 'Slave Interface' $i |awk '{print $NF}'|sort); do grep -A5 $j $i | grep -e 'Slave Interface' -e 'MII Status' -e 'Speed' | paste -s -d' '; done; }| xargs -I {} echo "${i}: {}"; done
EOF
}

check_imex_channel() {
  echo -e "\n########INFO: Check IMEX channel devices(Expected: At least one channel exist on GBXX)"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
[ $(uname -m) == aarch64 ] && { ls -1 /dev/nvidia-caps-imex-channels/ 2>/dev/null || true; } || true
EOF
}

check_imex_profile() {
  echo -e "\n########INFO: Check IMEX profile"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
[ $(uname -m) == aarch64 ] && { grep -E "^(SERVER_PORT|IMEX_CMD_PORT|IMEX_CMD_ENABLED|IMEX_CMD_BIND_INTERFACE_IP)=" /etc/nvidia-imex/config.cfg 2>/dev/null|paste -s -d ' '; } || true
EOF
}

check_imex_nodes_config() {
  echo -e "\n########INFO: Check IMEX nodes_config(Expected: file exist on GBXX)"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
[ $(uname -m) == aarch64 ] && { md5sum /etc/nvidia-imex/nodes_config.cfg 2>/dev/null || true; } || true
EOF
}

check_imex_service() {
  echo -e "\n########INFO: Check IMEX service status(Expected: 2 ports up and service is active on GBXX)"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
[ $(uname -m) == aarch64 ] && { ss -lntp|grep nvidia-imex|grep -v nv-hostengine|wc -l; systemctl is-active nvidia-imex.service || true; ls /cm/local/apps/slurm/var/*/*imex* 2>/dev/null||true; } || true
EOF
}

check_imex_readiness() {
  echo -e "\n#########INFO: Check IMEX readiness(Expected: imex is READY on GBXX)"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
[ $(uname -m) == aarch64 ] && { nvidia-imex-ctl -q 2>&1 || true; } || true
EOF
}

check_imex_connection() {
  echo -e "\n########INFO: Check IMEX connection status(Expected: Node be in READY, all nodes connected and Domain UP on GBXX)"
  pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
[ $(uname -m) == aarch64 ] && { nvidia-imex-ctl -N 2>&1 |awk '/Nodes:/,/Domain State:/{print $0}'|sed -e 's:\*: :g' -e 's:-::g'|tr -s ' '; } || true
EOF
}

check_enroot() {
  echo -e "\n########INFO: Check Enroot runtime directory(Expected: ENROOT_RUNTIME_PATH Writable)"
  local out; out="$(pdsh -f 100 -u 10 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
a=$(grep ENROOT_RUNTIME_PATH /etc/enroot/enroot.conf 2>/dev/null|grep -o /.\*/); if [ ! -z ${a} ]; then mkdir -p ${a} 2>/dev/null; touch ${a}/test &>/dev/null && (echo "${a} is writable"; rm -f ${a}/test) || echo "${a} is read-only"; else echo "ENROOT_RUNTIME_PATH not set"; fi
EOF
)"
  printf '%s\n' "$out"
  printf '%s' "$out" | grep -qi 'is read-only' && return 1 || return 0
}

check_dmesg_nv_err() {
  echo -e "\n########INFO: Check dmesg - NV_ERR(Expected: No NV_ERR_*)"
  local out; out="$(pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
S=; [ "$(id -u)" -ne 0 ] && sudo -n true 2>/dev/null && S="sudo -n"; $S dmesg|grep -o  NV_ERR_[^\]]*|sort|uniq
EOF
)"
  printf '%s\n' "$out"
  _detect_nonempty "$out" && return 1 || return 0
}

check_dmesg_call_trace() {
  echo -e "\n########INFO: Check dmesg - Call trace(Expected: No Call trace)"
  local out; out="$(pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF' | dshbak -c
S=; [ "$(id -u)" -ne 0 ] && sudo -n true 2>/dev/null && S="sudo -n"; $S dmesg|grep -i "Call trace"|wc -l
EOF
)"
  printf '%s\n' "$out"
  _detect_count_gt0 "$out" && return 1 || return 0
}

check_dmesg_hw_error() {
  echo -e "\n########INFO: Check dmesg - Hardware Error(Expected: No Hardware Error)"
  local out; out="$(pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF'|dshbak -c
S=; [ "$(id -u)" -ne 0 ] && sudo -n true 2>/dev/null && S="sudo -n"; $S dmesg|grep "Hardware Error"|wc -l
EOF
)"
  printf '%s\n' "$out"
  _detect_count_gt0 "$out" && return 1 || return 0
}

check_dmesg_xid() {
  echo -e "\n########INFO: Check dmesg - Xid(Expected: No Xid)"
  local out; out="$(pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF'|dshbak -c
S=; [ "$(id -u)" -ne 0 ] && sudo -n true 2>/dev/null && S="sudo -n"; $S dmesg|grep -E -o 'Xid ([^)].*): [0-9]+,'|tr -d ','|awk '{print $1":"$NF}'|sort|uniq|paste -s -d','
EOF
)"
  printf '%s\n' "$out"
  _detect_nonempty "$out" && return 1 || return 0
}

check_dmesg_knvlink() {
  echo -e "\n########INFO: Check dmesg - knvlinkDiscoverPostRxDetLinks(Expected: No knvlinkDiscoverPostRxDetLinks)"
  local out; out="$(pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF'|dshbak -c
S=; [ "$(id -u)" -ne 0 ] && sudo -n true 2>/dev/null && S="sudo -n"; $S dmesg|grep "NVRM: knvlinkDiscoverPostRxDetLinks"|wc -l
EOF
)"
  printf '%s\n' "$out"
  _detect_count_gt0 "$out" && return 1 || return 0
}

check_dmesg_ipmi_ssif() {
  echo -e "\n########INFO: Check dmesg - ipmi_ssif(Expected: No ipmi_ssif Not present errors)"
  local out; out="$(pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF'|dshbak -c
S=; [ "$(id -u)" -ne 0 ] && sudo -n true 2>/dev/null && S="sudo -n"; $S ipmitool mc info &>/dev/null||($S dmesg --since "8 hour ago" | grep "ipmi_ssif.*i2c-.*Not present"|wc -l)
EOF
)"
  printf '%s\n' "$out"
  _detect_count_gt0 "$out" && return 1 || return 0
}

check_dmesg_mlx5_health() {
  echo -e "\n########INFO: Check dmesg - mlx5_core.*Health issue observed.*ERROR(Expected: No mlx5_core Health issue observed errors)"
  local out; out="$(pdsh -f 100 -R ssh -w ${NODELIST} <<'EOF'|dshbak -c
S=; [ "$(id -u)" -ne 0 ] && sudo -n true 2>/dev/null && S="sudo -n"; $S dmesg --since "8 hour ago" | grep "mlx5_core.*Health issue observed.*ERROR"|wc -l
EOF
)"
  printf '%s\n' "$out"
  _detect_count_gt0 "$out" && return 1 || return 0
}

# ── Dispatch ───────────────────────────────────────────────────────────────────
_list_checks() {
  printf '%-24s %-5s %s\n' "ID" "KIND" "DESCRIPTION"
  local row id kind desc
  for row in "${CHECKS[@]}"; do
    IFS='|' read -r id kind desc <<< "$row"
    printf '%-24s %-5s %s\n' "$id" "$kind" "$desc"
  done
}

_require_nodelist() {
  [ -z "${NODELIST}" ] && NODELIST="${SLURM_JOB_NODELIST}"
  [ -z "${NODELIST}" ] && { echo "ERROR: NODELIST is not set. Provide a node list argument or run within a slurm job."; exit 1; }

  # Slurm commonly exposes compact hostlists such as rack-[11-12].  Passing
  # that expression directly to the Python ssh fanout treats it as one DNS
  # hostname and silently turns every hardware check into a lookup failure.
  # Resolve once, before any check runs, and retain the exact expanded nodes.
  local expanded
  command -v scontrol >/dev/null 2>&1 || {
    echo "ERROR: scontrol is required to expand NODELIST=${NODELIST}" >&2
    exit 1
  }
  expanded="$(scontrol show hostnames "${NODELIST}" 2>/dev/null)" || {
    echo "ERROR: unable to expand NODELIST=${NODELIST} through scontrol" >&2
    exit 1
  }
  NODELIST="$(printf '%s\n' "${expanded}" | awk 'NF' | paste -sd, -)"
  [ -n "${NODELIST}" ] || {
    echo "ERROR: scontrol returned no nodes for NODELIST=${POSITIONAL[0]:-${SLURM_JOB_NODELIST}}" >&2
    exit 1
  }
}

# Parse args: --check <name> / --check=<name> / --list, remaining positional = nodelist.
CHECK=""
DO_LIST=0
POSITIONAL=()
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK="$2"; shift 2 ;;
    --check=*) CHECK="${1#*=}"; shift ;;
    --list) DO_LIST=1; shift ;;
    --) shift; while [ $# -gt 0 ]; do POSITIONAL+=("$1"); shift; done ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done

if [ "${DO_LIST}" -eq 1 ]; then
  _list_checks
  exit 0
fi

NODELIST="${POSITIONAL[0]:-}"
_require_nodelist

if [ -n "${CHECK}" ]; then
  # Validate the check id against the registry.
  known=0
  for row in "${CHECKS[@]}"; do
    IFS='|' read -r id _ _ <<< "$row"
    [ "$id" = "${CHECK}" ] && { known=1; break; }
  done
  [ "$known" -eq 0 ] && { echo "ERROR: unknown check '${CHECK}'. Run: sysinfo.sh --list"; exit 2; }
  echo -e "########INFO: NODELIST=${NODELIST}"
  "check_${CHECK}"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo -e "\n########RESULT: ${CHECK} FAILED (exit ${rc})"
  else
    echo -e "\n########RESULT: ${CHECK} OK"
  fi
  exit "$rc"
fi

# ── Run ALL checks (legacy behavior: display only, exit 0) ─────────────────────
echo -e "\n########INFO: NODELIST=${NODELIST}"
for row in "${CHECKS[@]}"; do
  IFS='|' read -r id _ _ <<< "$row"
  "check_${id}" || true
done
echo
echo -e "\n########INFO: sysinfo check completed, please review the above information for any potential issue.\n"
exit 0
