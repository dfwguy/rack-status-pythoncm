#!/bin/bash
# Author: Junli Zhang<junliz@nvidia.com>
export topdir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ $(id -u) -ne 0 ] && echo "ERROR: Please run as root" && exit 1
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

## Caution: this script highly depends on pdsh to run command on all nodes, the BCM should be properly configured(or just leave with default settings) to let pdsh work without password.
export PDSH_SSH_ARGS_APPEND="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=quiet -o CheckHostIP=no"
export PDSH_RCMD_TYPE=ssh
export FANOUT=100
grep 'next unless "$tag" ne ""' /usr/bin/dshbak >/dev/null && sed -i.ori -e 's#next unless "$tag" ne "";#next unless defined $tag and "$tag" ne "";#g' /usr/bin/dshbak

NODELIST=$1
if [ -z ${NODELIST} ]; then
  NODELIST=${SLURM_JOB_NODELIST}
fi
[ -z ${NODELIST} ] && echo "ERROR: NODELIST is not set. Please provide node list as argument or run within a slurm job." && exit 1

## Don't depend on scontrol to get host list as it may cause issue when slurm is not healthy
#source /etc/profile && module unload slurm && module load slurm
#hosts=($(scontrol show hostname $NODELIST))
#[ ${#hosts[@]} -eq 0 ] && echo "ERROR: No hosts found from NODELIST: $NODELIST" && exit 1
echo -e "\n########INFO: NODELIST=${NODELIST}"

echo -e "\n########INFO: Check BIOS"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
grep -r . /sys/class/dmi/id/bios_* 2>/dev/null|cut -d'/' -f6-|sort|paste -s -d','
EOF

echo -e "\n########INFO: Check OS"
pdsh -t 5 -u 5 -f 100 -R ssh -w ${NODELIST} <<< "uname -r; df -Th|grep /cm/node-installer &>/dev/null || echo 'OS is READY'" 2>/dev/null | dshbak -c 

echo -e "\n########INFO: Check Kernel Parm"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
uname -r
cat /proc/cmdline | tr ' ' '\n'|grep -v -E 'ip=|BOOT' | sort | paste -s -d ' '
EOF

echo -e "\n########INFO: Check CPU"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
lscpu|awk '/Architecture:/,/L3 cache:/{print $0}'|grep -E -v 'GHz|MHz|BogoMIPS|Flags:|cache:'
EOF
echo -e "\n########INFO: Check CPU Frequency(Expected: CPUs are in performance mode and frequency all above 1800 MHz)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
if [ -f /sys/devices/system/cpu/cpufreq/policy0/scaling_governor ]; then cat /sys/devices/system/cpu/cpufreq/policy*/scaling_governor|sort|uniq -c; else echo "CPU frequency scaling not supported"; fi
[ $(uname -m) == x86_64 ] && { cpupower monitor -m Mperf|awk '/Mperf/,/^$/{print $0}' |grep -v -e Mperf -e Freq | awk -F '|' '{print $1,$NF}' |sort -n| awk '{key=$1; val=$2; sum[key]+=val;count[key]++} END{for(k in sum){avg=sum[k]/count[k]; status=(avg > 1800 ? "PASS" : "FAIL");printf "CPU Freq: Socket%s %s\n", k, status}}'|paste -s -d','; } || true
[ $(uname -m) == aarch64 ] && { m=; if [ -f /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq ]; then m=cpuinfo_cur_freq; else [ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq ] && m=scaling_cur_freq; fi; [ ! -z ${m} ] && { for i in $(ls -1 /sys/devices/system/node|grep node|sed -e 's:node::g'); do cat /sys/devices/system/node/node${i}/cpu*/cpufreq/$m 2>/dev/null|awk -v node=$i 'BEGIN{sum=0}{sum+=$1}END{if(NR>0){ret=int(sum/NR/1000); if(ret>1800){printf "%s freq PASS\n", node}else{printf "%s freq FAILED\n", node}}}'; done } || echo "CPU frequency info not available"; } || true
EOF

echo -e "\n########INFO: Check Memory"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
dmidecode -t memory|grep -P '\tVolatile Size:'|tr -d '\t'|sort|uniq -c
EOF
echo -e "\n########INFO: Check NUMA_BALANCING and THP(Expected: NUMA_BALANCING=0 and THP=madvise)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
sysctl kernel.numa_balancing
cat /sys/kernel/mm/transparent_hugepage/enabled
EOF

echo -e "\n########INFO: Check NUMA"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
numactl -H|grep size:|grep -v ' 0 MB'|awk '{print "N"$2"="int($(NF-1)/1000)"GB"}'|paste -s -d','
EOF

echo -e "\n########INFO: Check GPU basic"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
nvidia-smi --format=csv --query-gpu gpu_bus_id,gpu_name,vbios_version,pstate,enforced.power.limit,ecc.errors.uncorrected.aggregate.total,remapped_rows.failure,remapped_rows.uncorrectable,remapped_rows.pending
EOF

echo -e "\n#########INFO: Check NVLINK status(Expected: 72 links are up)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
nvidia-smi nvlink -s|grep '5[0-9].* GB/s'|wc -l
nvidia-smi --format=csv --query-gpu gpu_bus_id,fabric.state,fabric.status
EOF

echo -e "\n#########INFO: Check NVLINK BER(Expected: no output)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
nvidia-smi nvlink -e | grep BER | grep -v 15e-255 || true
EOF

echo -e "\n#########INFO: Check P2P nvlink capability status(Expected: All GPU are OK on GBXX/BXX/HXX)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
nvidia-smi topo -p2p n 2>&1|grep GPU[0-9].*OK||true
EOF
echo -e "\n#########INFO: Check P2P pcie capability status(Expected: All GPU are OK on RTX/L20)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
nvidia-smi topo -p2p p 2>&1|grep GPU[0-9].*OK||true
EOF

echo -e "\n########INFO: Check CUUID(Expected: For GBXX, all nodes in the same rack have the same CUUID)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
nvidia-smi -q | grep -E 'Bus Id|CliqueId|ClusterUUID'
EOF

echo -e "\n########INFO: Check IB devices status(Expected: All up and active)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
ibstatus|grep -E 'Infiniband|state:'|paste - - -|sort -t'_' -k2n
EOF

echo -e "\n########INFO: Check IB Link Down status(Expected: No issue found)"
#Bypass those 4 ports IB HCA is connected to nvswitch on B200/B300.
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
ls -l /sys/class/infiniband|grep -v $((lspci -D | grep -i Infiniband|awk '{print $1}'|cut -d '.' -f 1|sort|uniq -c|grep -w 4||echo NONE)|awk '{print $NF}'|xargs -I {} echo "-e {}.")|grep infiniband|awk -F'/' '{print $(NF-2),$NF}'|while read bdf dev; do ret=$(mlxlink -d $bdf 2>&1|grep Recommendation|cut -f2- -d ':'|grep -v 'No issue was observed'||true); [ "X$ret" != "X" ] && echo "$dev:${ret}"; done|paste -s -d','
EOF

echo -e "\n########INFO: Check IB Link Down counters(Expected: No issue found)"
#Bypass those 4 ports IB HCA is connected to nvswitch on B200/B300.
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
for dev in $(ls -l /sys/class/infiniband|grep -v $((lspci -D | grep -i Infiniband|awk '{print $1}'|cut -d '.' -f 1|sort|uniq -c|grep -w 4||echo NONE)|awk '{print $NF}'|xargs -I {} echo "-e {}.")|grep infiniband|awk -F'/' '{print $NF}'); do link_downed=$(</sys/class/infiniband/${dev}/ports/1/counters/link_downed); symbol_error=$(</sys/class/infiniband/${dev}/ports/1/counters/symbol_error); if [[ ${link_downed} -ne 0 || ${symbol_error} -ne 0 ]]; then echo ${dev}:link_downed=${link_downed},symbol_error=${symbol_error}; fi; done
EOF

echo -e "\n########INFO: Check IB PKEY(Expected: All nodes have the same PKEY)"
#0x8003 in this example, then the pkey will be 0x0003: 0x8 -> 0x0
#Value	Base P_Key	Membership	Meaning
#0x7fff	0x7fff	    limited	    Default partition, limited member 
#0xffff	0x7fff	    full	      Same partition as 0x7fff, but full member 
#0x8100	0x0100	    full	      Non-default partition ID 0x0100, full member
#Communication rules (same base P_Key only):
#Full ↔ Full: allowed
#Full ↔ Limited: allowed
#Limited ↔ Limited (same partition): not allowed
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
for i in $(ls -l /sys/class/infiniband|grep -v $((lspci -D | grep -i Infiniband|awk '{print $1}'|cut -d '.' -f 1|sort|uniq -c|grep -w 4||echo NONE)|awk '{print $NF}'|xargs -I {} echo "-e {}.")|grep infiniband|awk -F'/' '{print $NF}'|grep -v bond_|sort -t'_' -k2n); do ret=$(cat /sys/class/infiniband/${i}/ports/1/pkeys/*|grep -E -v '0x0000'|sort|uniq|paste -s -d','); [ ! -z $ret ] && echo "${i}: ${ret}"; done || true
EOF

echo -e "\n########INFO: Check IB FW(Expected: All nodes have the same FW version)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
for i in /sys/class/infiniband/*; do printf "%50s%15s\n" ${i}/fw_ver $(<$i/fw_ver); done
EOF

echo -e "\n########INFO: Check PCIe LnkCap+LnkStat(Expected: No issue found, but on BXX/HXX xx:xx.{0..3} shown is expected)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
[ $(uname -m) == x86_64 ] && { lspci -d 15b3: -nn|awk '{print $1}'|while read i; do [ $(lspci -s $i -vvv|grep -e LnkCap: -e LnkSta:|grep -o Speed.*|awk -F',' '{print $1, $2}'|tr -s ' '|sort|uniq -c|wc -l) -ne 1 ] && echo $i || true; done; } || true
[ $(uname -m) == aarch64 ] && { lspci -d 15b3: -nn|grep controller|awk '{print $1}'|while read i; do [ $(lspci -s $i -vvv|grep -e LnkCap: -e LnkSta:|grep -o Speed.*|awk -F',' '{print $1, $2}'|tr -s ' '|sort|uniq -c|wc -l) -ne 1 ] && echo $i || true; done; } || true
EOF
echo -e "\n########INFO: Check PCIe ACS(Expected: All same brand nodes the same)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
lspci -vvv|grep 'ACSCtl:.*SrcValid-'|wc -l
EOF
echo -e "\n########INFO: Check PCIe MRRS(Expected: all 4096, but on BXX/HXX some<4> with 512 is expected)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
lspci -d 15b3: -nn|awk '{print $1}'|xargs -I {} lspci -s {} -vvv|grep -o MaxReadReq.*|awk '{print $(NF-1)}'|sort|uniq -c
EOF

echo -e "\n########INFO: Check IFace device status(Expected: all nodes use the same interface connect to default gateway)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
ip r sh|grep default|grep -o dev.*|cut -f2 -d' '
EOF

echo -e "\n########INFO: Check Bond interfaces(Expected: all nodes the same)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
for i in /proc/net/bonding/*; do { ethtool ${i##*/} | grep -e Speed -e Link; echo '==DETAILS=='; grep -e 'Bonding Mode' -e 'Transmit Hash Policy' $i; for j in $(grep 'Slave Interface' $i |awk '{print $NF}'|sort); do grep -A5 $j $i | grep -e 'Slave Interface' -e 'MII Status' -e 'Speed' | paste -s -d' '; done; }| xargs -I {} echo "${i}: {}"; done
EOF

echo -e "\n########INFO: Check IMEX channel devices(Expected: At least one channel exist on GBXX)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
[ $(uname -m) == aarch64 ] && { ls -1 /dev/nvidia-caps-imex-channels/ 2>/dev/null || true; } || true
EOF

echo -e "\n########INFO: Check IMEX profile"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
[ $(uname -m) == aarch64 ] && { grep -E "^(SERVER_PORT|IMEX_CMD_PORT|IMEX_CMD_ENABLED|IMEX_CMD_BIND_INTERFACE_IP)=" /etc/nvidia-imex/config.cfg 2>/dev/null|paste -s -d ' '; } || true
EOF

echo -e "\n########INFO: Check IMEX nodes_config(Expected: file exist on GBXX)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
[ $(uname -m) == aarch64 ] && { md5sum /etc/nvidia-imex/nodes_config.cfg 2>/dev/null || true; } || true
EOF

echo -e "\n########INFO: Check IMEX service status(Expected: 2 ports up and service is active on GBXX)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
[ $(uname -m) == aarch64 ] && { ss -lntp|grep nvidia-imex|grep -v nv-hostengine|wc -l; systemctl is-active nvidia-imex.service || true; ls /cm/local/apps/slurm/var/*/*imex* 2>/dev/null||true; } || true
EOF

echo -e "\n#########INFO: Check IMEX readiness(Expected: imex is READY on GBXX)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
[ $(uname -m) == aarch64 ] && { nvidia-imex-ctl -q 2>&1 || true; } || true
EOF

echo -e "\n########INFO: Check IMEX connection status(Expected: Node be in READY, all nodes connected and Domain UP on GBXX)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
[ $(uname -m) == aarch64 ] && { nvidia-imex-ctl -N 2>&1 |awk '/Nodes:/,/Domain State:/{print $0}'|sed -e 's:\*: :g' -e 's:-::g'|tr -s ' '; } || true
EOF

echo -e "\n########INFO: Check Enroot runtime directory(Expected: ENROOT_RUNTIME_PATH Writable)"
pdsh -f 100 -u 10 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
a=$(grep ENROOT_RUNTIME_PATH /etc/enroot/enroot.conf 2>/dev/null|grep -o /.\*/); if [ ! -z ${a} ]; then mkdir -p ${a} 2>/dev/null; touch ${a}/test &>/dev/null && (echo "${a} is writable"; rm -f ${a}/test) || echo "${a} is read-only"; else echo "ENROOT_RUNTIME_PATH not set"; fi
EOF

echo -e "\n########INFO: Check dmesg - NV_ERR(Expected: No NV_ERR_*)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
dmesg|grep -o  NV_ERR_[^\]]*|sort|uniq
EOF

echo -e "\n########INFO: Check dmesg - Call trace(Expected: No Call trace)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF' | dshbak -c
dmesg|grep -i "Call trace"|wc -l
EOF

echo -e "\n########INFO: Check dmesg - Hardware Error(Expected: No Hardware Error)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF'|dshbak -c
dmesg|grep "Hardware Error"|wc -l
EOF

echo -e "\n########INFO: Check dmesg - Xid(Expected: No Xid)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF'|dshbak -c
dmesg|grep -E -o 'Xid ([^)].*): [0-9]+,'|tr -d ','|awk '{print $1":"$NF}'|sort|uniq|paste -s -d','
EOF

echo -e "\n########INFO: Check dmesg - knvlinkDiscoverPostRxDetLinks(Expected: No knvlinkDiscoverPostRxDetLinks)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF'|dshbak -c
dmesg|grep "NVRM: knvlinkDiscoverPostRxDetLinks"|wc -l
EOF

echo -e "\n########INFO: Check dmesg - ipmi_ssif(Expected: No ipmi_ssif Not present errors)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF'|dshbak -c
ipmitool mc info &>/dev/null||(dmesg --since "8 hour ago" | grep "ipmi_ssif.*i2c-.*Not present"|wc -l)
EOF

echo -e "\n########INFO: Check dmesg - mlx5_core.*Health issue observed.*ERROR(Expected: No mlx5_core Health issue observed errors)"
pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF'|dshbak -c
dmesg --since "8 hour ago" | grep "mlx5_core.*Health issue observed.*ERROR"|wc -l
EOF

#echo -e "\n########INFO: Check dmesg - NVRM:.*Assertion failed(Expected: No NVRM Assertion failed errors)"
#pdsh -f 100 -R ssh -w ${NODELIST} <<- 'EOF'|dshbak -c
#dmesg --since "1 hour ago" | grep "NVRM:.*Assertion failed"|wc -l
#EOF

echo
echo -e "\n########INFO: sysinfo check completed, please review the above information for any potential issue.\n"

