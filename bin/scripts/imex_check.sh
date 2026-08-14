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
# Runs all and only IMEX-related checks from sysinfo.sh

export topdir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ "$(id -u)" -ne 0 ] && echo "ERROR: Please run as root" && exit 1
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

export PDSH_SSH_ARGS_APPEND="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=quiet -o CheckHostIP=no"
export PDSH_RCMD_TYPE=ssh
export FANOUT=100
grep 'next unless "$tag" ne ""' /usr/bin/dshbak >/dev/null && sed -i.ori -e 's#next unless "$tag" ne "";#next unless defined $tag and "$tag" ne "";#g' /usr/bin/dshbak

NODELIST="$1"
if [ -z "${NODELIST}" ]; then
  NODELIST="${SLURM_JOB_NODELIST}"
fi
[ -z "${NODELIST}" ] && echo "ERROR: NODELIST is not set. Please provide node list as argument or run within a slurm job." && exit 1

echo -e "\n########INFO: NODELIST=${NODELIST}"

echo -e "\n########INFO: Check IMEX channel devices(Expected: At least one channel exist on GBXX)"
pdsh -f 100 -R ssh -w "${NODELIST}" <<- 'EOF' | dshbak -c
[ "$(uname -m)" == aarch64 ] && { ls -1 /dev/nvidia-caps-imex-channels/ 2>/dev/null || true; } || true
EOF

echo -e "\n########INFO: Check IMEX profile"
pdsh -f 100 -R ssh -w "${NODELIST}" <<- 'EOF' | dshbak -c
[ "$(uname -m)" == aarch64 ] && { grep -E "^(SERVER_PORT|IMEX_CMD_PORT|IMEX_CMD_ENABLED|IMEX_CMD_BIND_INTERFACE_IP)=" /etc/nvidia-imex/config.cfg 2>/dev/null | paste -s -d ' '; } || true
EOF

echo -e "\n########INFO: Check IMEX nodes_config(Expected: file exist on GBXX)"
pdsh -f 100 -R ssh -w "${NODELIST}" <<- 'EOF' | dshbak -c
[ "$(uname -m)" == aarch64 ] && { md5sum /etc/nvidia-imex/nodes_config.cfg 2>/dev/null || true; } || true
EOF

echo -e "\n########INFO: Check IMEX service status(Expected: 2 ports up and service is active on GBXX)"
pdsh -f 100 -R ssh -w "${NODELIST}" <<- 'EOF' | dshbak -c
[ "$(uname -m)" == aarch64 ] && { ss -lntp | grep nvidia-imex | grep -v nv-hostengine | wc -l; systemctl is-active nvidia-imex.service || true; ls /cm/local/apps/slurm/var/*/*imex* 2>/dev/null || true; } || true
EOF

echo -e "\n#########INFO: Check IMEX readiness(Expected: imex is READY on GBXX)"
pdsh -f 100 -R ssh -w "${NODELIST}" <<- 'EOF' | dshbak -c
[ "$(uname -m)" == aarch64 ] && { nvidia-imex-ctl -q 2>&1 || true; } || true
EOF

echo -e "\n########INFO: Check IMEX connection status(Expected: Node be in READY, all nodes connected and Domain UP on GBXX)"
pdsh -f 100 -R ssh -w "${NODELIST}" <<- 'EOF' | dshbak -c
[ "$(uname -m)" == aarch64 ] && { nvidia-imex-ctl -N 2>&1 | awk '/Nodes:/,/Domain State:/{print $0}' | sed -e 's:\*: :g' -e 's:-::g' | tr -s ' '; } || true
EOF

echo
echo -e "\n########INFO: imex check completed, please review the above information for any potential issue.\n"

