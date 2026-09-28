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
# lib/nvswitch_creds.inc.sh — resolve NVSwitch NVOS / BMC admin credentials from the environment.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Source (do not execute). The site password for the switch `admin` (NVOS) and BMC
# `root` accounts used to live as a literal in four of these scripts, so it shipped
# to every clone and stayed in git history permanently. It is now read from the
# environment, and a missing value is a hard error rather than a silent fallback —
# a wrong-or-empty password against a BMC locks the account out.
#
#   RACK_NVSW_ADMIN_PASSWORD   NVOS `admin` password (required)
#   RACK_NVSW_BMC_PASSWORD     BMC `root` password (optional; defaults to the above)
#
# The RACK_ prefix is deliberate: transport/factory.py forwards RACK_* over SSH, so
# these reach the head node without being added to an allowlist by hand.

# Populate NVSW_ADMIN_PASSWORD, or exit 78 (EX_CONFIG) naming the variable.
nvsw_require_admin_password() {
	NVSW_ADMIN_PASSWORD=${RACK_NVSW_ADMIN_PASSWORD:-}
	if [[ -z "$NVSW_ADMIN_PASSWORD" ]]; then
		cat >&2 <<-EOF
		${0##*/}: RACK_NVSW_ADMIN_PASSWORD is not set.

		The NVOS admin password is no longer stored in this repository. Export it
		before running, or set it in the deployment environment:

		    read -rsp 'NVOS admin password: ' RACK_NVSW_ADMIN_PASSWORD
		    export RACK_NVSW_ADMIN_PASSWORD

		Refusing to continue: an empty password can lock out the switch account.
		EOF
		exit 78
	fi
}

# Populate NVSW_BMC_PASSWORD, falling back to the NVOS admin password.
nvsw_require_bmc_password() {
	NVSW_BMC_PASSWORD=${RACK_NVSW_BMC_PASSWORD:-${RACK_NVSW_ADMIN_PASSWORD:-}}
	if [[ -z "$NVSW_BMC_PASSWORD" ]]; then
		cat >&2 <<-EOF
		${0##*/}: neither RACK_NVSW_BMC_PASSWORD nor RACK_NVSW_ADMIN_PASSWORD is set.

		Export the BMC root password before running:

		    read -rsp 'BMC root password: ' RACK_NVSW_BMC_PASSWORD
		    export RACK_NVSW_BMC_PASSWORD

		Refusing to continue: PATCHing an empty password would lock out BMC root.
		EOF
		exit 78
	fi
}
