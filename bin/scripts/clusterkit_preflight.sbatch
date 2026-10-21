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
#SBATCH --job-name=ClusterKit-preflight
#SBATCH --no-requeue
set -euo pipefail
ulimit -c 0
[[ "$#" == 2 ]] || { echo 'ERROR: requires prepared plan and reviewed script directory' >&2; exit 2; }
exec python3 "$2/clusterkit_execute.py" batch-preflight --plan "$1"
