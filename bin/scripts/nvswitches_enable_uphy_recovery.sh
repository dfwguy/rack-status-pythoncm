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
export SSHPASS="$NVSW_ADMIN_PASSWORD"

sshpass -e pdsh -w $1 -R ssh "nv set fae int acp1-144 link phy-recovery serdes-eq-mode enabled;
                              nv config apply -y;
			      nv config save;"

#check
#
# before ct reset/reboot
#   p3-r15-nvsw-08: serdes-eq-mode     disabled     enabled
#   p3-r15-nvsw-08: serdes-eq-timeout  0            0
#
# after ct reset/reboot
#   p3-r15-nvsw-08: serdes-eq-mode     enabled     enabled
#   p3-r15-nvsw-08: serdes-eq-timeout  100          0
#sshpass -e pdsh -w $1 -R ssh "seq 1 144 | xargs -i -t nv show fae int acp{} link phy-recovery;"
