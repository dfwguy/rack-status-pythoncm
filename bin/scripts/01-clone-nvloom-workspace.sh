#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# NVIS Rack Status — clone NVIDIA nvloom into TOPDIR.
# MUST run on the dashboard / Flask host (localhost) only — install-nvloom-workspace.sh runs this before any pdsh.
# Never invoke this script on compute via pdsh; clone uses git and network from the head node into shared $HOME.
#
# Usage: 01-clone-nvloom-workspace.sh [TOPDIR]
set -euo pipefail

TOPDIR="${1:-/home/cmsupport/workspace}"
NVLOOM="${TOPDIR}/nvloom"

echo "==> nvloom clone on localhost: host=$(hostname 2>/dev/null || echo unknown)"

mkdir -p "$TOPDIR"

if [[ -d "${NVLOOM}/.git" ]]; then
  echo "==> nvloom: already cloned at ${NVLOOM}, git pull --ff-only"
  git -C "$NVLOOM" pull --ff-only
elif [[ -e "$NVLOOM" ]]; then
  echo "ERROR: ${NVLOOM} exists but is not a git repository." >&2
  exit 1
else
  echo "==> nvloom: git clone https://github.com/NVIDIA/nvloom → ${NVLOOM}"
  git clone https://github.com/NVIDIA/nvloom.git "$NVLOOM"
fi

echo "OK: nvloom sources at ${NVLOOM}"
