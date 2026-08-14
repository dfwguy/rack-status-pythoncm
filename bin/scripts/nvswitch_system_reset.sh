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
# Full system factory reset on one NV switch (NVOS). Args: RACK_ID NVSWITCH_HOSTNAME
# Example: nvswitch_system_reset.sh p01-r01 p01-r01-nvsw-03
set -euo pipefail
RACK="${1:?rack id required}"
HOST="${2:?nvswitch hostname required}"
case "$HOST" in
"${RACK}"-nvsw-*) ;;
*)
	echo "${0##*/}: second argument must be ${RACK}-nvsw-* (got: ${HOST})" >&2
	exit 1
	;;
esac
pdsh -w "$HOST" "nv action reset system factory-default force"
