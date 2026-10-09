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

# nvis-rack-status may run --help without root (GET /api/hw-check-help sets RACK_WEB_PRINT_HELP=1).
if [ $(id -u) -ne 0 ] && [ "${RACK_WEB_PRINT_HELP:-}" != "1" ]; then
  echo "ERROR: Please run as root"
  exit 1
fi

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export topdir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

global_status=0
reason=""

cpu_expected_count=144
memory_expected_size_gb_per_socket=480
gpu_expected_count=4
nvlink_expected_count=$[gpu_expected_count*18]
bf3_expected_count=2
ib_expected_count=4
dmesg_hours_to_look_back=8

f_set_cpu_freq_userspace(){
  for i in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo userspace > $i; done
  for i in /sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq; do echo 3330000 > $i; done
  for i in /sys/devices/system/cpu/cpu*/cpufreq/scaling_min_freq; do echo 3000000 > $i; done
  for i in /sys/devices/system/cpu/cpu*/cpufreq/scaling_setspeed; do echo 3200000 > $i; done
  echo 0 > /sys/devices/system/cpu/cpufreq/boost
  ret=($(cat /sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_cur_freq|sort -n)); echo CPUFREQ: "MIN=$[${ret[0]}/1000] MAX=$[${ret[-1]}/1000]"
}

f_check_cpu(){
  ## CPU
  if [ $(grep processor /proc/cpuinfo|wc -l) -ne ${cpu_expected_count} ]; then
    msg="ERROR: CPU CORES is not ${cpu_expected_count}"
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
  cpupower -c all frequency-set -g performance &>/dev/null
  if [ $(grep -v performance /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor|wc -l) -ne 0 ]; then
    msg='ERROR: CPU governor is not performance'
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
  m=; if [ -f /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq ]; then m=cpuinfo_cur_freq; else [ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq ] && m=scaling_cur_freq; fi
  [ ! -z ${m} ] && for i in $(ls -1 /sys/devices/system/node|grep node|sed -e 's:node::g'); do 
    j=$(cat /sys/devices/system/node/node${i}/cpu*/cpufreq/$m 2>/dev/null|awk 'BEGIN{sum=0}{sum+=$1}END{if(NR>0){print int(sum/NR/1000)}}')
    if [[ ! -z ${j} && ${j} -lt 1800 ]]; then
      msg="ERROR: CPU NUMA${i} average frequency is abnormal(<1800MHz)"
      echo $msg
      reason="${reason} ${msg}"
      global_status=$[global_status+1]
    fi
  done
}

f_check_mem(){
  ## MEM
  if [ $(dmidecode -t memory|grep -P '\tSize:'|tr -d '\t'|grep ${memory_expected_size_gb_per_socket}|wc -l) -ne 2 ]; then
    msg="ERROR: Memory size is not 2*${memory_expected_size_gb_per_socket}GB"
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
}

f_check_gpu(){
  ## GPU
  if [ $(lspci | grep '3D controller' | wc -l) -ne ${gpu_expected_count} ]; then
    msg="ERROR: GPU number is not ${gpu_expected_count}"
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
  if [ $(lspci | grep '3D controller' | grep 'rev ff' | wc -l) -gt 0 ]; then
    msg='ERROR: GPU met ref ff issue'
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
  if `nvidia-smi -L` &>/dev/null; then
    msg="ERROR: GPU driver didn't load correctly"
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
  ##GPU ECC
  ret=($(nvidia-smi --format=csv --query-gpu gpu_bus_id,ecc.errors.uncorrected.aggregate.total|grep -v pci.bus_id|tr ',' ' '|while read bus_id ecc; do [[ X${ecc} != 'X[N/A]' && $ecc -ne 0 ]] && echo $bus_id; done))
  if [ ${#ret[*]} -ne 0 ]; then
    msg="WARN: GPU ECC: $(echo ${ret[*]})"
    echo $msg
  fi
  ret=($(nvidia-smi --format=csv --query-gpu gpu_bus_id,ecc.errors.uncorrected.aggregate.total|grep -v pci.bus_id|tr ',' ' '|while read bus_id ecc; do [[ X${ecc} == 'X[N/A]' ]] && echo $bus_id; done))
  if [ ${#ret[*]} -ne 0 ]; then
    msg="FATAL: GPU ECC=[N/A]: $(echo ${ret[*]})"
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
  ##GPU Clocks
  #ret=($(nvidia-smi --format=csv,noheader,nounits --query-gpu clocks.sm,clocks.max.sm|tr -d ','|sort -n -k 1|head -n 1))
  #if [ ${ret[0]} -ne ${ret[1]} ]; then
  #  msg="WARN: GPU sm clock hasn't been fixed to max"
  #  echo $msg
  #fi
  ret=$(nvidia-smi --format=csv,noheader,nounits --query-gpu pstate|tr -d 'P'|awk 'BEGIN{sum=0}{sum+=$1}END{print sum}')
  if [ $ret -ne 0 ]; then
    msg="WARN: GPUs are not all in P0 state"
    echo $msg
  fi
}

f_check_nvlink(){
  if [ $(nvidia-smi nvlink -s|grep '5[0-9].* GB/s'|wc -l) -ne ${nvlink_expected_count} ]; then
    msg="ERROR: ${nvlink_expected_count} GPU NVLINKs are not fully connected"
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
  if [ $(nvidia-smi --format=csv --query-gpu gpu_bus_id,fabric.state,fabric.status|grep 'Completed.*Success'|wc -l
  ) -ne ${gpu_expected_count} ]; then
    msg='ERROR: GPU NVLINK fabric are not all in good status'
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
  if [ $(nvidia-smi topo -p2p n|grep GPU[0-9].*OK|grep -v NS|wc -l) -ne ${gpu_expected_count} ]; then
    msg='ERROR: GPU P2P all not fully connected'
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
}

f_check_imex(){
  ## IMEX
  if [ $(ls -1 /dev/nvidia-caps-imex-channels/|wc -l) -lt 1 ]; then
    msg='ERROR: IMEX channel doesnt exist'
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
  if [ ! -f /etc/nvidia-imex/nodes_config.cfg ]; then
    msg='ERROR: IMEX /etc/nvidia-imex/nodes_config.cfg doesnt exist. Restart nvidia-imex service'
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
  if [ "X$(nvidia-imex-ctl -q 2>/dev/null)" != "XREADY" ]; then
    msg='ERROR: IMEX is not active'
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
}

f_check_disk(){
  ##Enroot disk check
  enroot_runtime_path=$(grep ENROOT_RUNTIME_PATH /etc/enroot/enroot.conf|grep -o /.\*/)
  if [ ! -d $enroot_runtime_path ]; then
    enroot_runtime_path=$(df -Th|grep '/dev/md'|awk '{print $NF}'|head -n1)
    if [ "X${enroot_runtime_path}" == "X" ]; then
      enroot_runtime_path=/root
    fi
  fi
  touch $enroot_runtime_path/test &>/dev/null
  ret=$?
  if [ $ret -ne 0 ]; then
    msg="ERROR: Can't write to enroot runtime dir: $enroot_runtime_path"
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  else
    rm -f $enroot_runtime_path/test
  fi
}

f_check_nfs(){
  ## NFS
  if `df -Th|grep master:/home` &>/dev/null; then
    msg='ERROR: home not mounted'
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
  if `df -Th|grep master:/cm/shared` &>/dev/null; then
    msg='ERROR: cmshared not mounted'
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
}

f_check_cuda(){
  #CUDA Test
  if [ -x ${topdir}/cudatest/foo ]; then
    timeout 3 ${topdir}/cudatest/foo &>/dev/null
    ret=$?
    if [ $ret -ne 0 ]; then
      msg='ERROR: Cannot run even a simple CUDA app'
      echo $msg
      reason="${reason} ${msg}"
      global_status=$[global_status+1]
    fi
  fi
}

f_check_ns_bf3(){
  if [ $(lspci | grep 'Ethernet.*BlueField-3'|wc -l) -lt ${bf3_expected_count} ]; then
    msg="WARN: BlueField-3 ethernet ports number is not ${bf3_expected_count}"
    echo $msg
  fi
  ns_nics=($(lspci -D|grep 'Ethernet.*BlueField-3'|awk '{print $1}'|while read i; do echo $(basename $(ls -l /sys/class/net|grep -o ${i}.*) 2>/dev/null);done))
  ret=$(for i in ${ns_nics[*]}; do lldpcli show neighbors ports $i; done | grep -E 'Interface:|SysName:|PortID:'|awk '/Interface:/{print $2} /SysName:/{print $NF} /PortID:/{print $NF}'|tr ',' ' '|paste - - -|sort|paste -s -d ' ' -|awk '{if(NF==6 && $3 != $6){print $0," WARN"}else{print $0}}'|tr '\t' ' '|tr -s ' '|grep -w WARN |wc -l)
  if [ $ret -ne 0 ]; then
    msg="WARN: BlueField-3 ethernet ports connected to different switch ports"
    echo $msg
  fi
  ret=$(mst start &>/dev/null; for i in $(mst status -v|grep BlueField3|awk '{print $2}'); do echo "$i $(mlxconfig -d ${i} -e query INTERNAL_CPU_OFFLOAD_ENGINE | grep INTERNAL_CPU_OFFLOAD_ENGINE)"; done|awk '{print $(NF-1)}'|grep ENABLED|wc -l)
  if [ $ret -ne 0 ]; then
    msg="WARN: BlueField-3 INTERNAL_CPU_OFFLOAD_ENGINE is not disabled on all ports"
    echo $msg
  fi
}

f_check_ew_ib(){
  ## IB
  if [ $(lspci | grep 'Infiniband controller'|wc -l) -ne ${ib_expected_count} ]; then
    msg="ERROR: IB HCA number is not ${ib_expected_count}"
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
  active_hcas=($(lspci -D|grep 'Infiniband controller'|awk '{print $1}'|while read i; do hca=$(basename $(ls -l /sys/class/infiniband|grep -o ${i}.*) 2>/dev/null); [ ! -z $hca ] && grep ACTIVE /sys/class/infiniband/${hca}/ports/1/state &> /dev/null && echo ${hca}:1;done))
  if [ ${#active_hcas[*]} -ne ${ib_expected_count} ]; then
    msg="ERROR: Active HCA number is not ${ib_expected_count}" # $(echo ${active_hcas[*]}|tr ' ' ',')
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
  hcas=($(lspci -D|grep 'Infiniband controller'|awk '{print $1}'|while read i; do echo $(basename $(ls -l /sys/class/infiniband|grep -o ${i}.*) 2>/dev/null);done))
  ret=($(
  for dev in ${hcas[*]}; do
    link_downed=$(</sys/class/infiniband/${dev}/ports/1/counters/link_downed)
    symbol_error=$(</sys/class/infiniband/${dev}/ports/1/counters/symbol_error)
    if [[ ${link_downed} -ne 0 || ${symbol_error} -ne 0 ]]; then
      echo ${dev}:link_downed=${link_downed},symbol_error=${symbol_error}
    fi
  done
  ))
  if [ ${#ret[*]} -ne 0 ]; then
    echo "WARN: ${ret[*]}"
  fi
  ret=($(
  for dev in ${hcas[*]}; do
    link_w=$(mlxlink -d ${dev} --port_type pcie|grep 'Width.*16X '|wc -l)
    if [[ ${link_w} -lt 1 ]]; then
      echo "${dev}"
    fi
  done
  ))
  if [ ${#ret[*]} -ne 0 ]; then
    echo "WARN: LinkWidth:${ret[*]}"
  fi
  ret=($(
  lspci | grep 'Infiniband controller'|awk '{print $1}'|while read i; do
   MRRS=$(lspci -s $i -vvv|grep -o MaxReadReq.*bytes|awk '{print $2}')
   if [[ ${MRRS} -lt 4096 ]]; then
      echo "${i}"
   fi
  done
  ))
  if [ ${#ret[*]} -ne 0 ]; then
    echo "WARN: MaxReadReq!=4096:${ret[*]}"
  fi
}

f_check_ew_spx(){
  ## SPX
  ret=($(
  # Every RDMA device, not just mlx5_*: GB300 Spectrum-X compute rails are VFs named roce_vf_r*.
  # Devices without the counter (IB HCAs, bonds) are skipped rather than read as an error.
  for dev in /sys/class/infiniband/*; do
    [[ -r ${dev}/ports/1/hw_counters/roce_adp_retrans ]] || continue
    roce_adp_retrans=$(<${dev}/ports/1/hw_counters/roce_adp_retrans)
    if [[ ${roce_adp_retrans} -ne 0 ]]; then
      echo $(basename ${dev}):roce_adp_retrans=${roce_adp_retrans}
    fi
  done
  ))
  if [ ${#ret[*]} -ne 0 ]; then
    msg="WARN: SPX RoCE ADP retransmissions: ${ret[*]}"
    echo $msg
  fi
  active_hcas=($(lspci -D|grep 'Ethernet controller:.*Mellanox'|awk '{print $1}'|while read i; do hca=$(basename $(ls -l /sys/class/infiniband|grep -o ${i}.*) 2>/dev/null); [ ! -z $hca ] && grep ACTIVE /sys/class/infiniband/${hca}/ports/1/state &> /dev/null && echo ${hca}:1;done))
  if [ ${#active_hcas[*]} -ne $(ls -1 /sys/class/infiniband|wc -l) ]; then
    msg="WARN: Active HCA number is not $(ls -1 /sys/class/infiniband|wc -l)"
    echo $msg
  fi
  mst start &>/dev/null;
  hcas=($(lspci -D|grep -E '(Infiniband|Ethernet) controller:.*Mellanox'|awk '{print $1}'|while read i; do echo $(basename $(ls -l /sys/class/infiniband|grep -o ${i}.*) 2>/dev/null);done))
  ret=($(
  for dev in ${hcas[*]}; do
    #link_downed=$(</sys/class/infiniband/${dev}/ports/1/counters/link_downed)
    #symbol_error=$(</sys/class/infiniband/${dev}/ports/1/counters/symbol_error)
    #if [[ ${link_downed} -ne 0 || ${symbol_error} -ne 0 ]]; then
    #  echo ${dev}:link_downed=${link_downed},symbol_error=${symbol_error}
    #fi
    tmp_c=($(mlxlink -d ${dev} -c|grep -E 'Effective Physical Errors|Link Down Counter'|awk '{print $NF}'|sed 's:N/A:0:g'))
    link_downed=${tmp_c[1]}
    effective_physical_errors=${tmp_c[0]}
    #echo ${dev}:link_downed=${link_downed},effective_physical_errors=${effective_physical_errors}
    if [[ ${link_downed} -ne 0 || ${effective_physical_errors} -ne 0 ]]; then
      echo ${dev}:link_downed=${link_downed},effective_physical_errors=${effective_physical_errors}
    fi
  done
  ))
  if [ ${#ret[*]} -ne 0 ]; then
    echo "WARN: ${ret[*]}"
  fi
  #lldpcli show neighbors | grep -E 'Interface:|SysName:|PortID:'|awk '/Interface:/{print $2} /SysName:/{print $NF} /PortID:/{print $NF}'|tr ',' ' '|paste - - -|sort -k2|paste - -|awk '{if(NF==6 && $3 != $6){print $0," WARN"}else{print $0}}'
}

f_check_dmesg(){
  ##AER
  #if [ $(dmesg --since "${dmesg_hours_to_look_back} hour ago" | grep 'pcieport.*AER:'|wc -l) -ne 0 ]; #then
  #    msg='WARN: pcieport AER shown in dmesg'
  #    echo $msg
  #fi
  ##NV_ERR_INVALID_STATE and so on
  ret=($(dmesg --since "48 hour ago" |grep -o  NV_ERR_[^\]]*|sort|uniq))
  if [ ${#ret[*]} -ne 0 ]; then
      msg="ERROR: ${ret[*]} shown in dmesg"
      echo $msg
      reason="${reason} ${msg}"
      global_status=$[global_status+1]
  fi
  ## Hardware Error
  if [ $(dmesg --since "${dmesg_hours_to_look_back} hour ago" | grep 'Hardware Error'|wc -l) -ne 0 ]; then
      msg='WARN: Hardware Error shown in dmesg'
      echo $msg
  fi
  ## DBE
  if [ $(dmesg --since "${dmesg_hours_to_look_back} hour ago" | grep 'Xid.*uncorrectable double bit error'|wc -l) -ne 0 ]; then
      msg='WARN: uncorrectable double bit error shown in dmesg'
      echo $msg
  fi
  ## knvlinkDiscoverPostRxDetLinks
  if [ $(dmesg --since "${dmesg_hours_to_look_back} hour ago" | grep 'NVRM: knvlinkDiscoverPostRxDetLinks'|wc -l) -ne 0 ]; then
      msg='ERROR: knvlinkDiscoverPostRxDetLinks error shown in dmesg'
      echo $msg
      reason="${reason} ${msg}"
      global_status=$[global_status+1]
  fi
  ## ipmi i2c errors
  if [ $(dmesg --since "${dmesg_hours_to_look_back} hour ago" | grep 'ipmi_ssif.*i2c-'|wc -l) -gt 5 ]; then
      msg='WARN: ipmi_ssif i2c errors shown in dmesg'
      echo $msg
  fi
  ## Xid
  ## https://docs.nvidia.com/deploy/xid-errors/analyzing-xid-catalog.html
  if [ $(dmesg | grep 'Xid'|wc -l) -gt 2 ]; then
      msg="WARN: Xid shown in dmesg: $(dmesg|grep -E -o 'Xid ([^)].*): [0-9]+,'|tr -d ','|awk '{print $1":"$NF}'|sort|uniq|paste -s -d',')"
      echo $msg
  fi
  ## Call trace:
  if [ $(dmesg --since "${dmesg_hours_to_look_back} hour ago" | grep -i 'Call trace:'|wc -l) -gt 0 ]; then
      msg='WARN: Call trace shown in dmesg'
      echo $msg
  fi
  ## mlx5_core.*Health issue observed.*ERROR
  if [ $(dmesg|grep 'mlx5_core.*Health issue observed.*ERROR'|wc -l) -ne 0 ]; then
      msg='ERROR: mlx5_core.*Health issue observed error shown in dmesg'
      echo $msg
      reason="${reason} ${msg}"
      global_status=$[global_status+1]
  fi
}


f_check_dcgm_diag(){
  # MOP GB300 §7.1.1 step 2 — standardised on -r 3 (NscaleTX decision 2026-07-26;
  # MOP text says -r 2, FastTrack says -r 4).
  local lvl="${dcgm_diag_level:-3}"
  if ! command -v dcgmi &>/dev/null; then
    echo "WARN: dcgmi not found, skip DCGM diag"
    return 0
  fi
  echo "INFO: running dcgmi diag -r ${lvl} (this can take several minutes)"
  local out
  out=$(dcgmi diag -r "${lvl}" 2>&1)
  echo "${out}"
  if echo "${out}" | grep -qiE '\bfail\b|error'; then
    echo "ERROR: dcgmi diag -r ${lvl} reported failures"
    reason="${reason} dcgm_diag_r${lvl}_fail"
    global_status=$((global_status+1))
  else
    echo "INFO: dcgmi diag -r ${lvl} PASSED"
  fi
}

f_check_nvbandwidth(){
  # MOP incubation/nvbandwidth-single-node-mop.md; FastTrack §7 pre-flight tests.
  #
  # nvbandwidth is NOT shipped as a node binary — the MOP delivers it as an OCI container
  # imported to a shared .sqsh ("Delivery: OCI container ... launched via Slurm + Pyxis +
  # Enroot"). The original `command -v nvbandwidth` test therefore skipped on every node,
  # forever, while still printing "Health check PASSED". So: try the binary, then fall back
  # to running the staged container with enroot directly (this script runs ON the tray, so
  # srun is not available to us — enroot is the right tool here).
  local NVB_RUN=""  NVB_MODE=""  ctr="" sq=""

  if command -v nvbandwidth &>/dev/null; then
    NVB_RUN="nvbandwidth"; NVB_MODE="binary"
  else
    for sq in ${NVIS_NVBANDWIDTH_SQSH:-} \
              "${NVIS_NODE_CONTAINER_DIR:-/tmp/containers}"/nvbandwidth-v0.9-arm.sqsh \
              "${NVIS_NODE_CONTAINER_DIR:-/tmp/containers}"/nvbandwidth*.sqsh \
              /cm/shared/containers/nvbandwidth*.sqsh \
              /cm/shared/apps/llmb/images/nvbandwidth*.sqsh; do
      [[ -s "${sq}" ]] && { ctr="${sq}"; break; }
    done
    if [[ -n "${ctr}" ]] && command -v enroot &>/dev/null; then
      NVB_MODE="container:${ctr}"
    fi
  fi

  if [[ -z "${NVB_MODE}" ]]; then
    # Loud and unambiguous. Deliberately NOT counted as a failure (that could drain the
    # node), but it must never read as a silent pass.
    echo "WARN: *** nvbandwidth SKIPPED — no binary on PATH and no .sqsh found ***"
    echo "WARN: *** THIS NODE WAS NOT BANDWIDTH-TESTED. 'PASSED' below excludes nvbandwidth. ***"
    echo "WARN: stage the image (MOP §4.1) or set NVIS_NVBANDWIDTH_SQSH=/path/to.sqsh"
    return 0
  fi
  echo "INFO: nvbandwidth mode=${NVB_MODE}"

  local ename="" t rc
  if [[ "${NVB_MODE}" == container:* ]]; then
    ename="nvbw_$$_${RANDOM}"
    # MELLANOX_VISIBLE_DEVICES=none: enroot's 99-mellanox.sh hook tries to bind
    # /sys/class/infiniband_mad, which does not exist on these trays — it exits 1 and the
    # container never starts. Same fault that killed MNUBERGEMM job 62114.
    if ! enroot create --name "${ename}" "${ctr}" >/dev/null 2>&1; then
      echo "ERROR: enroot create failed for ${ctr}"
      reason="${reason} nvbandwidth_container_create_fail"
      global_status=$((global_status+1))
      return 0
    fi
    NVB_RUN="env NVIDIA_VISIBLE_DEVICES=all MELLANOX_VISIBLE_DEVICES=none enroot start ${ename} nvbandwidth"
  fi

  for t in device_to_device_memcpy_read_ce \
           device_to_device_bidirectional_memcpy_read_ce \
           host_to_device_memcpy_ce; do
    echo "INFO: nvbandwidth -t ${t}"
    ${NVB_RUN} -t "${t}" 2>&1; rc=$?
    if [ ${rc} -ne 0 ]; then
      echo "ERROR: nvbandwidth ${t} failed (rc=${rc})"
      reason="${reason} nvbandwidth_${t}_fail"
      global_status=$((global_status+1))
    fi
  done

  [[ -n "${ename}" ]] && enroot remove -f "${ename}" >/dev/null 2>&1
  return 0
}

f_help(){
  echo "$(basename $0)"
  echo "  options:"
  echo "    -d | --dry-run     <Run check only, will not drain node if error met>"
  echo "    -e | --extra-check <Check the dmesg to see if some potensial issues, 1:enable, 0:disable. Default: enable>"
  echo "    -s | --check-sys   <Check CPU/Mem/NFS, 1:enable, 0:disable. Default: enable>"
  echo "    -g | --check-gpu   <Check GPU/NVLINK/CUDA, 1:enable, 0:disable. Default: enable>"
  echo "    -i | --check-imex  <Check IMEX, 1:enable, 0:disable. Default: disable>"
  echo "    -b | --check-ib    <Check IB, 1:enable, 0:disable. Default: enable>"
  echo "    -x | --check-spx   <Check SPX, 1:enable, 0:disable. Default: disable>"
  echo "    -p | --check-process   <Check processes, 1:enable, 0:disable. Default: disable>"
  echo "    -D | --check-dcgm-diag <Run dcgmi diag, 1:enable, 0:disable. Default: disable>"
  echo "    -L | --dcgm-diag-level <dcgmi diag level r1-r4. Default: 3>"
  echo "    -N | --check-nvbandwidth <Run nvbandwidth pre-flight, 1:enable, 0:disable. Default: disable>"
  echo "    -S | --check-slurm-stack <slurmd/munge/prolog/epilog/pyxis/enroot, 1:enable. Default: disable>"
  echo "    -h | --help"
  exit 0
}

f_check_slurm_stack(){
  ## Job-launch stack: slurmd, munge, prolog/epilog, pyxis SPANK plugin, enroot runtime.
  ## WHY: job 63850 (MNUBERGEMM, 18 trays) was CANCELLED with Reason=Prolog at RunTime 00:00:00.
  ## Every GPU/NVLink/CUDA check on that tray passed - the fault was entirely in the launch
  ## stack, which nothing here covered. A tray that cannot start a job is not "healthy".
  local st plug prolog epilog SC CFG
  ## sudo -n env ... strips PATH and SLURM_CONF (see hw_check_pdsh.sh), so `scontrol show config`
  ## silently returns NOTHING and every field parses as empty. The first version of this check
  ## reported that as "no Prolog configured" -> PASSED, on a tray whose job had just died with
  ## Reason=Prolog. Resolve both explicitly, and treat an unreadable config as an ERROR.
  SC=$(command -v scontrol 2>/dev/null)
  [ -x "$SC" ] || SC=$(ls -1 /cm/local/apps/slurm/*/bin/scontrol 2>/dev/null | tail -1)
  [ -x "$SC" ] || SC=$(ls -1 /cm/local/apps/slurm/current/bin/scontrol 2>/dev/null | tail -1)
  if [ -z "${SLURM_CONF}" ]; then
    export SLURM_CONF=$(ls -1 /cm/shared-*/apps/slurm/etc/slurm/slurm.conf /cm/shared/apps/slurm/etc/slurm/slurm.conf 2>/dev/null | head -1)
  fi
  if [ -z "$SC" ] || [ ! -x "$SC" ]; then
    msg='ERROR: scontrol not found on this node - cannot verify Prolog/Epilog/pyxis'
    echo $msg; global_status=$[global_status+1]; return
  fi
  CFG=$("$SC" show config 2>&1)
  if [ $? -ne 0 ] || [ -z "$CFG" ] || ! echo "$CFG" | grep -q '='; then
    msg="ERROR: 'scontrol show config' returned nothing (SLURM_CONF=${SLURM_CONF:-unset}) - Prolog/Epilog/pyxis state UNKNOWN, not healthy"
    echo $msg; global_status=$[global_status+1]; return
  fi
  echo "INFO: slurm config readable (SLURM_CONF=${SLURM_CONF:-default}, scontrol=${SC})"

  for svc in slurmd munge; do
    st=$(systemctl is-active ${svc} 2>/dev/null)
    if [ "X${st}" != "Xactive" ]; then
      msg="ERROR: ${svc} is not active (systemctl is-active = ${st:-unknown})"
      echo $msg; global_status=$[global_status+1]
    else
      echo "INFO: ${svc} active"
    fi
    # A unit can be 'active' and still have failed its last start attempt.
    if systemctl is-failed ${svc} &>/dev/null; then
      msg="ERROR: ${svc} is in a failed state"; echo $msg; global_status=$[global_status+1]
    fi
  done

  ## ---- LOCAL slurm.conf Prolog (READ-ONLY diagnostic) -----------------------------------
  ## `scontrol show config` queries SLURMCTLD, not this node's file — which is why it reported
  ## "no Prolog configured" on trays whose slurmd was demonstrably failing a prolog. slurmd runs
  ## the Prolog from its LOCAL slurm.conf. Read that file directly.
  _lc="${SLURM_CONF}"
  [ -r "${_lc}" ] || _lc=$(ls -1 /etc/slurm/slurm.conf /cm/local/apps/slurm/var/etc/slurm.conf 2>/dev/null | head -1)
  if [ -r "${_lc}" ]; then
    _lp=$(grep -iE '^[[:space:]]*Prolog[[:space:]]*=' "${_lc}" | tail -1 | cut -d= -f2- | tr -d '[:space:]')
    if [ -n "${_lp}" ]; then
      echo "INFO: LOCAL slurm.conf Prolog = ${_lp}  (from ${_lc})"
      if [ ! -f "${_lp}" ]; then
        msg="ERROR: local Prolog script MISSING on this node: ${_lp}"
        echo $msg; global_status=$[global_status+1]
      elif [ ! -x "${_lp}" ]; then
        msg="ERROR: local Prolog script NOT EXECUTABLE: ${_lp}"
        echo $msg; global_status=$[global_status+1]
      else
        echo "INFO: local Prolog present+executable ($(stat -c '%A %U:%G %s bytes' "${_lp}" 2>/dev/null))"
        ## BCM's prolog dispatches a directory of parts; a single failing part is the usual cause.
        for _d in "${_lp}.d" /cm/local/apps/cmd/scripts/prolog.d /etc/slurm/prolog.d; do
          [ -d "${_d}" ] || continue
          echo "INFO: prolog parts in ${_d}: $(ls -1 "${_d}" 2>/dev/null | tr '\n' ' ')"
          for _f in "${_d}"/*; do
            [ -f "${_f}" ] || continue
            [ -x "${_f}" ] || { msg="ERROR: prolog part NOT EXECUTABLE: ${_f}"; echo $msg; global_status=$[global_status+1]; }
          done
        done
      fi
    else
      echo "INFO: no Prolog= line in ${_lc}"
    fi
  else
    echo "WARNING: could not read a local slurm.conf to check Prolog="
  fi
  ## Widen the journal window and keep MORE context — the earlier 3-line tail cut off the
  ## prolog's own stderr, which is the line that actually says why it failed.
  if command -v journalctl >/dev/null 2>&1; then
    _pctx=$(journalctl -u slurmd --since "-24h" --no-pager 2>/dev/null \
            | grep -iE "prolog" | tail -12)
    [ -n "${_pctx}" ] && { echo "INFO: ---- last 12 prolog journal lines (24h) ----"; echo "${_pctx}"; }
  fi

  ## ---- NODE STATE / DRAIN REASON (controller-side) --------------------------------------
  ## THE THING THIS CHECK EXISTED FOR AND ORIGINALLY MISSED.
  ## "Prolog error" is not visible in the tray's slurm.conf — the trays report *no Prolog
  ## configured*. It is slurmctld's DRAIN REASON, held in controller node state. So a tray whose
  ## jobs are being killed by the prolog looked perfectly healthy here. Ask the controller.
  _me=$(hostname -s 2>/dev/null || hostname)
  _ns=$("$SC" show node "${_me}" 2>/dev/null)
  if [ -z "${_ns}" ]; then
    msg="ERROR: scontrol show node ${_me} returned nothing - node state UNKNOWN"
    echo $msg; global_status=$[global_status+1]
  else
    _state=$(echo "${_ns}" | awk -F'State=' 'NF>1{print $2}' | awk '{print $1}')
    _reason=$(echo "${_ns}" | sed -n 's/.*Reason=\(.*\)$/\1/p' | head -1)
    echo "INFO: slurm node state = ${_state:-unknown}"
    case "${_state}" in
      *DRAIN*|*DOWN*|*FAIL*|*INVAL*)
        msg="ERROR: node is ${_state} in Slurm — Reason: ${_reason:-none given}"
        echo $msg; global_status=$[global_status+1]
        ;;
      *)
        [ -n "${_reason}" ] && echo "INFO: slurm reason: ${_reason}"
        ;;
    esac
    ## A drain reason can be cleared while the underlying fault remains. Surface any recent
    ## prolog/epilog failure from the local journal too — that is the tray-side evidence.
    if command -v journalctl >/dev/null 2>&1; then
      _pj=$(journalctl -u slurmd --since "-2h" --no-pager 2>/dev/null \
            | grep -iE "prolog|epilog" | grep -iE "error|fail|timeout" | tail -3)
      if [ -n "${_pj}" ]; then
        msg="ERROR: slurmd journal shows prolog/epilog failures in the last 2h: $(echo "${_pj}" | tr '\n' ' | ')"
        echo $msg; global_status=$[global_status+1]
      else
        echo "INFO: no prolog/epilog errors in slurmd journal (last 2h)"
      fi
    fi
  fi

  ## Prolog/Epilog: read the paths Slurm ACTUALLY uses, do not assume the BCM default.
  for kind in Prolog Epilog; do
    path=$(echo "$CFG" | awk -v k="^${kind}[ ]*=" '$0 ~ k {print $3}')
    if [ "X${path}" == "X" ] || [ "X${path}" == "X(null)" ]; then
      ## "(null)" from a CONFIRMED-readable config genuinely means none is set.
      echo "INFO: no ${kind} configured (confirmed from scontrol show config)"
      continue
    fi
    if [ ! -f "${path}" ]; then
      msg="ERROR: ${kind} script missing: ${path}"; echo $msg; global_status=$[global_status+1]
    elif [ ! -x "${path}" ]; then
      msg="ERROR: ${kind} script not executable: ${path}"; echo $msg; global_status=$[global_status+1]
    else
      echo "INFO: ${kind} present and executable: ${path}"
      ## Dry-run it. This is the ONLY check that reproduces the actual failure mode: a prolog
      ## that exits non-zero drains the node and cancels the whole allocation.
      if [ ${dry_run} -eq 0 ]; then
        out=$(SLURM_JOB_ID=0 SLURM_JOB_USER=${USER:-root} timeout 60 "${path}" 2>&1); rc=$?
        if [ ${rc} -ne 0 ]; then
          msg="ERROR: ${kind} exited ${rc} on dry execution: $(echo "$out"|tail -3|tr '\n' ' ')"
          echo $msg; global_status=$[global_status+1]
        else
          echo "INFO: ${kind} dry execution returned 0"
        fi
      fi
    fi
  done

  ## Pyxis is a SPANK plugin; if plugstack cannot load it every containerised job dies at launch.
  plug=$(echo "$CFG" | awk '/^PlugStackConfig/ {print $3}')
  [ "X${plug}" == "X" ] && plug=/etc/slurm/plugstack.conf
  if [ -f "${plug}" ] || [ -d "${plug}.d" ]; then
    if grep -rqs pyxis "${plug}" "${plug}.d" 2>/dev/null; then
      so=$(grep -rhs pyxis "${plug}" "${plug}.d" 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i ~ /\.so$/) print $i}' | head -1)
      if [ "X${so}" != "X" ] && [ ! -f "${so}" ]; then
        msg="ERROR: pyxis plugin declared but .so missing: ${so}"; echo $msg; global_status=$[global_status+1]
      else
        echo "INFO: pyxis SPANK plugin configured${so:+ (${so})}"
      fi
    else
      ## Only a real signal when a plugstack file EXISTS and omits pyxis.
      echo "WARNING: no pyxis entry in ${plug} - verify containerised jobs launch"
    fi
  else
    ## PlugStackConfig=(null) means Slurm uses its compiled-in default path; BCM wires pyxis
    ## elsewhere. PROVEN NOT FATAL: h01 ran MNUBERGEMM (job 63870) in a pyxis/enroot container at
    ## 72 kW with this exact condition on all 18 trays. Informational only - do NOT fail the node.
    echo "INFO: PlugStackConfig unset (${plug}) - Slurm default path; pyxis not verifiable from here"
  fi

  ## Enroot: binary + writable runtime/cache/data dirs. NFS cannot do overlayfs whiteout mknod,
  ## so a runtime path on NFS breaks every container even though the dir looks writable.
  if ! command -v enroot &>/dev/null; then
    msg='ERROR: enroot not found in PATH'; echo $msg; global_status=$[global_status+1]
  else
    echo "INFO: enroot $(enroot version 2>/dev/null|head -1)"
    for v in ENROOT_RUNTIME_PATH ENROOT_CACHE_PATH ENROOT_DATA_PATH; do
      d=$(enroot config 2>/dev/null | awk -v k="^${v}" '$0 ~ k {print $2}')
      [ "X${d}" == "X" ] && continue
      d=$(eval echo "${d}")
      if [ ! -d "${d}" ]; then
        echo "INFO: ${v}=${d} (not yet created)"
      elif ! touch "${d}/.hcheck" 2>/dev/null; then
        msg="ERROR: ${v} not writable: ${d}"; echo $msg; global_status=$[global_status+1]
      else
        rm -f "${d}/.hcheck"
        fstype=$(stat -f -c %T "${d}" 2>/dev/null)
        if [ "X${fstype}" == "Xnfs" ]; then
          msg="ERROR: ${v} is on NFS (${d}) - overlayfs whiteout will fail for every container"
          echo $msg; global_status=$[global_status+1]
        else
          echo "INFO: ${v}=${d} writable (${fstype})"
        fi
      fi
    done
  fi
}

f_check_process(){
  if [ $(nvidia-smi --query-compute-apps=pid --format=csv,noheader,nounits|grep -v '[N/A]'|wc -l) -gt 0 ]; then
    msg='INFO: GPUs are being used by processes'
    echo $msg
  fi
}

f_check_pcie_config(){
  ## Check ACS
  # for BDF in $(lspci -d "*:*:*" | awk '{print $1}'); do lspci -vvvv -nn -s $BDF|grep -e 'ACSCtl:.*SrcValid' -e $BDF | paste - -; done | grep 'ACSCtl:.*SrcValid-'
  ret=$(lspci -vvv|grep 'ACSCtl:.*SrcValid-'|wc -l)
  if [ $ret -eq 0 ]; then
    msg='ERROR: ACS is not fully enabled on all devices'
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
  ## Check MRRS
  # lspci -d 15b3: -nn|awk '{print $1}'|xargs -I {} lspci -s {} -vvv|grep -o MaxReadReq.*|sort|uniq -c
  ret=($(lspci -d 15b3: -nn|awk '{print $1}'|xargs -I {} lspci -s {} -vvv|grep -o MaxReadReq.*|awk '{print $(NF-1)}'|sort|uniq))
  if [[ ${#ret[*]} -eq 0 || $(echo ${ret[*]}|tr ' ' '\n'|grep -v 4096|wc -l) -ne 0 ]]; then
    msg='ERROR: MRRS is not set to 4096 on all Mellanox devices'
    echo $msg
    reason="${reason} ${msg}"
    global_status=$[global_status+1]
  fi
}

###### Main
# nvis-rack-status invokes with RACK_WEB_HW_CHECK=1 and may pass no args (use defaults).
[[ $# -eq 0 && "${RACK_WEB_HW_CHECK:-}" == "1" ]] && set -- -e 1
[[ $# -eq 0 ]] && f_help && exit 0

extra_check=1

check_cpu=1
check_mem=1
check_nfs=1
check_disk=1
check_pcie_config=1

check_gpu=1
check_nvlink=1
check_cuda=1

check_imex=0

check_ew_ib=1
check_ew_spx=0
check_process=0
check_dcgm_diag=0
check_nvbandwidth=0
check_slurm_stack=0
dcgm_diag_level=3

dry_run=0
# D:/L:/N: were MISSING here while the -D/-L/-N case arms, the f_check_dcgm_diag /
# f_check_nvbandwidth functions and their dispatch all existed. getopt therefore rejected
# them, and because its exit status was unchecked the script carried on with the surviving
# options and printed "Health check PASSED" having run nothing. The dcgm_diag, nvbandwidth
# and mop_preflight presets were silent no-ops reporting success.
options=d,e:,s:,g:,i:,b:,x:,p:,h,D:,L:,N:,S:
optionl=dry-run,extra-check:,check-sys:,check-gpu:,check-imex:,check-ib:,check-spx:,check-process:,help,check-dcgm-diag:,dcgm-diag-level:,check-nvbandwidth:,check-slurm-stack:
# Fail loudly on an unparsable option instead of silently running a subset. A health checker
# that reports PASSED without executing its checks is worse than one that errors.
if ! OPTS=$(getopt -a -n "$0" --options $options --longoptions $optionl -- "$@"); then
  echo "ERROR: invalid options: $*" >&2
  exit 2
fi
eval set -- "$OPTS"
while :
do
  case "$1" in
    -d | --dry-run )
      dry_run=1
      shift 1
      ;;
    -e | --extra-check )
      extra_check="$2"
      shift 2
      ;;
    -s | --check-sys )
      check_cpu="$2"
      check_mem="$2"
      check_nfs="$2"
      check_disk="$2"
      shift 2
      ;;
    -g | --check-gpu )
      check_gpu="$2"
      check_nvlink="$2"
      check_cuda="$2"
      shift 2
      ;;
    -i | --check-imex )
      check_imex="$2"
      shift 2
      ;;
    -b | --check-ib )
      check_ew_ib="$2"
      shift 2
      ;;
    -x | --check-spx )
      check_ew_spx="$2"
      shift 2
      ;;
    -h | --help)
      f_help
      exit 0
      ;;
    -p | --check-process )
      check_process="$2"
      shift 2
      ;;
    -D | --check-dcgm-diag )
      check_dcgm_diag="$2"
      shift 2
      ;;
    -L | --dcgm-diag-level )
      dcgm_diag_level="$2"
      shift 2
      ;;
    -S | --check-slurm-stack )
      check_slurm_stack="$2"
      shift 2
      ;;
    -N | --check-nvbandwidth )
      check_nvbandwidth="$2"
      shift 2
      ;;
    --)
      shift;
      break
      ;;
    *)
      f_help
      exit 0
      ;;
  esac
done

if [ $(mount|grep /cm/node-installer|wc -l) -ne 0 ]; then
  echo "INFO: Running on installer node, skip some checks"
  exit 0
fi

[ ${extra_check} -eq 1 ] && f_check_dmesg
[ ${check_cpu} -eq 1 ] && f_check_cpu
[ ${check_mem} -eq 1 ] && f_check_mem
[ ${check_nfs} -eq 1 ] && f_check_nfs
[ ${check_disk} -eq 1 ] && f_check_disk
[ ${check_gpu} -eq 1 ] && f_check_gpu
[ ${check_nvlink} -eq 1 ] && f_check_nvlink
[ ${check_cuda} -eq 1 ] && f_check_cuda
[ ${check_imex} -eq 1 ] && f_check_imex
[ ${check_pcie_config} -eq 1 ] && f_check_pcie_config
[ ${check_ew_ib} -eq 1 ] && f_check_ew_ib
[ ${check_ew_spx} -eq 1 ] && f_check_ew_spx
[ ${check_process} -eq 1 ] && f_check_process
[ ${check_dcgm_diag} -eq 1 ] && f_check_dcgm_diag
[ ${check_nvbandwidth} -eq 1 ] && f_check_nvbandwidth
[ ${check_slurm_stack} -eq 1 ] && f_check_slurm_stack
## Print health check status
if [ ${global_status} -ne 0 ]; then
  echo 'INFO: Health check FAILED'
else
  echo 'INFO: Health check PASSED'
fi

set +x
# Never drain/undrain Slurm nodes when launched from nvis-rack-status (pdsh health check).
if [ "${RACK_WEB_HW_CHECK:-}" = "1" ]; then
  exit $(( global_status > 255 ? 255 : global_status ))
fi
source /etc/profile
module purge
module load slurm
if [ ${dry_run} -ne 1 ]; then
  if [ ${global_status} -ne 0 ]; then
    scontrol update nodename=$(hostname) stat=drain reason="${reason}"
  else
    if `scontrol show node $(hostname)|grep State=|grep -i DRAIN &>/dev/null`; then
      scontrol update nodename=$(hostname) stat=undrain
    fi
  fi
fi
