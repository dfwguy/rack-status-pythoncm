#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=bin/scripts/lib/nvswitch_credentials.sh
source "${HERE}/lib/nvswitch_credentials.sh"

usage() {
	echo "Usage: ${0##*/} <NVSwitch hostname ending in -nvsw-01>" >&2
	exit 33
}

run_switch_ssh() {
	local host=$1
	shift
	sshpass -f "$SSH_PASSWORD_FILE" ssh -o BatchMode=no "admin@${host}" "$@"
}

is_nmx_c_ok() {
	local host=$1
	if run_switch_ssh "$host" "nv show cluster apps nmx-controller | grep -F 'status            ok'" >/dev/null; then
		echo "${host}: NMX C is OK"
		return 0
	fi
	echo "${host}: NMX C is KO"
	return 1
}

configure_nmx_c() {
	local host=$1
	if is_nmx_c_ok "$host"; then
		return 0
	fi

	echo "configuring NMX C"
	run_switch_ssh "$host" "sudo rm -f /host/cluster_infra/app_config/nmx-controller/fm_config/nmx-controller_fm_config*
nv action generate sdn config app nmx-controller type fm_config
sudo sed -i 's/^MNNVL_TOPOLOGY=.*/MNNVL_TOPOLOGY=gb200_nvl72r1_c2g4_topology/' /host/cluster_infra/app_config/nmx-controller/fm_config/nmx-controller_fm_config*
nv action install sdn config app nmx-controller type fm_config files \$(basename /host/cluster_infra/app_config/nmx-controller/fm_config/nmx-controller_fm_config*)
nv action stop cluster apps nmx-controller
nv action start cluster apps nmx-controller"

	echo "Sleeping 60 sec while it is starting..."
	sleep 60
	is_nmx_c_ok "$host"
}

[[ $# -eq 1 ]] || usage
[[ "$1" == *-nvsw-01 ]] || {
	echo "Refusing to configure NMX-C: target must end in -nvsw-01" >&2
	exit 1
}
command -v sshpass >/dev/null 2>&1 || {
	echo "sshpass is required" >&2
	exit 69
}
nvis_secret_file NVSWITCH_SSH_PASSWORD_FILE /etc/nvis-rack-status/secrets/nvswitch-ssh.password
SSH_PASSWORD_FILE=$REPLY
configure_nmx_c "$1"
