#!/bin/bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

#SBATCH --job-name=CK-entire-SU
#SBATCH --exclusive
#SBATCH --ntasks-per-node=1
#SBATCH --time=08:00:00

set -euo pipefail
: "${RACK_CLUSTERKIT_SUITE_TEST_TYPE:=entire_SU}"
# shellcheck disable=SC1091
. "${NVIS_BIN_SCRIPTS}/clusterkit_suite_common.inc.sh"
