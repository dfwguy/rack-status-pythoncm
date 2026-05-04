#!/bin/bash
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
  for dev in /sys/class/infiniband/mlx5_*; do
    roce_adp_retrans=$(</sys/class/infiniband/$(basename ${dev})/ports/1/hw_counters/roce_adp_retrans)
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
  echo "    -h | --help"
  exit 0
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

dry_run=0
options=d,e:,s:,g:,i:,b:,x:,p:,h
optionl=dry-run,extra-check:,check-sys:,check-gpu:,check-imex:,check-ib:,check-spx:,check-process:,help
OPTS=$(getopt -a -n $0 --options $options --longoptions $optionl -- "$@")
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
