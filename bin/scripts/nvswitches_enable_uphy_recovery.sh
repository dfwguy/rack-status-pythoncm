#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=bin/scripts/lib/nvswitch_credentials.sh
source "${HERE}/lib/nvswitch_credentials.sh"

usage() {
	echo "Usage: ${0##*/} <NVSwitch nodeset>" >&2
	exit 2
}

[[ $# -eq 1 && -n "$1" ]] || usage
command -v sshpass >/dev/null 2>&1 || {
	echo "sshpass is required" >&2
	exit 69
}
command -v pdsh >/dev/null 2>&1 || {
	echo "pdsh is required" >&2
	exit 69
}

nvis_secret_file NVSWITCH_SSH_PASSWORD_FILE /etc/nvis-rack-status/secrets/nvswitch-ssh.password
SSH_PASSWORD_FILE=$REPLY

sshpass -f "$SSH_PASSWORD_FILE" pdsh -w "$1" -R ssh \
	"nv set fae int acp1-144 link phy-recovery serdes-eq-mode enabled;
nv config apply -y;
nv config save;"
