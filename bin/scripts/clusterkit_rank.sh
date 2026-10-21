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
# One Slurm task, all paths are positional literal arguments from the reviewed plan.
set -eo pipefail
[[ "$#" == 3 ]] || exit 2
init=$(python3 "$3/clusterkit_execute.py" init-path --plan "$1")
export HPCX_ENABLE_NCCL_MRC_PLUGIN=0 HPCX_ENABLE_NCCL_SPCX_PLUGIN=0
source "$init"
hpcx_load
exec python3 "$3/clusterkit_execute.py" node --plan "$1" --stage "$2"
