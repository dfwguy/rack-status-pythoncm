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

rack="$1"
if [[ -z "$rack" ]]; then
  echo "usage: $0 <rack id>" >&2
  exit 2
fi

pdsh -w "${rack}-ct[01-18]" 'dcgm_service=cuda-dcgm; systemctl cat cuda-dcgm >/dev/null 2>&1 || dcgm_service=nvidia-dcgm; systemctl stop cmd; systemctl restart "${dcgm_service}"; sleep 2; systemctl start cmd; sleep 10; systemctl restart nvidia-imex'
