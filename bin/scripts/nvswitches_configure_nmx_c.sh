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
#

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=lib/nvswitch_creds.inc.sh
. "$HERE/lib/nvswitch_creds.inc.sh"
nvsw_require_admin_password

function is_nmx_c_ok
{
sshpass -p "$NVSW_ADMIN_PASSWORD" ssh admin@$ip_os \
	"nv show cluster apps nmx-controller |grep 'status            ok'" \
	&& echo "$ip_os: NMX C is OK" \
	|| echo "$ip_os: NMX C is KO"
}



function configure_nmx_c
{
    if test -z "$1"; then
        echo "Configure one switch."
        echo "Usage: $(basename $0) <NVOS.IP.ADD.RESS>"
        exit 33
    fi

    ip_os=$1
     
    is_nmx_c_ok |grep OK && exit 0
    echo "configuring NMX C"
    sshpass -p "$NVSW_ADMIN_PASSWORD" ssh admin@$ip_os \
    "sudo rm /host/cluster_infra/app_config/nmx-controller/fm_config/nmx-controller_fm_config*  
	nv action generate sdn config app nmx-controller type fm_config
sudo sed -i 's/^MNNVL_TOPOLOGY=.*/MNNVL_TOPOLOGY=gb200_nvl72r1_c2g4_topology/' /host/cluster_infra/app_config/nmx-controller/fm_config/nmx-controller_fm_config*
nv action install sdn config app nmx-controller type fm_config files \$(basename /host/cluster_infra/app_config/nmx-controller/fm_config/nmx-controller_fm_config*)
nv action stop cluster apps nmx-controller
nv action start cluster apps nmx-controller"                                                       

    echo "Sleeping 60 sec  while it is starting..."
    sleep 60
   
    is_nmx_c_ok 
}

echo "$1" | grep -q "nvsw-01" || { echo "The name of the switch has not nsvw-01 you might be trying to configure NMX-C on the wrong device " >&2; exit 1; }
configure_nmx_c $* 
exit

