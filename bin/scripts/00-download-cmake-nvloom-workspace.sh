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
# NVIS Rack Status — download and extract Kitware CMake (linux-aarch64) into TOPDIR.
# Run on the dashboard / Flask host (localhost) only — install-nvloom-workspace.sh invokes this before pdsh.
# The tarball targets aarch64 compute nodes; this host may be x86_64 (download + extract still succeed).
#
# Usage: 00-download-cmake-nvloom-workspace.sh [TOPDIR]
set -euo pipefail

TOPDIR="${1:-/home/cmsupport/workspace}"

CMAKE_VER="3.28.1"
CMAKE_TAR="cmake-${CMAKE_VER}-linux-aarch64.tar.gz"
CMAKE_URL="https://github.com/Kitware/CMake/releases/download/v${CMAKE_VER}/${CMAKE_TAR}"
CMAKE_ROOT="${TOPDIR}/cmake-${CMAKE_VER}-linux-aarch64"

mkdir -p "$TOPDIR"
cd "$TOPDIR"

if [[ -x "${CMAKE_ROOT}/bin/cmake" ]]; then
  echo "OK: workspace CMake ${CMAKE_VER} already present (${CMAKE_ROOT})"
  exit 0
fi

echo "==> nvloom (localhost): download CMake ${CMAKE_VER} (linux-aarch64 tarball) → ${TOPDIR}"
if [[ ! -f "$CMAKE_TAR" ]]; then
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --retry-delay 2 -o "$CMAKE_TAR" "$CMAKE_URL"
  elif command -v wget >/dev/null 2>&1; then
    wget -c -O "$CMAKE_TAR" "$CMAKE_URL"
  else
    echo "ERROR: need curl or wget in PATH to download CMake." >&2
    exit 1
  fi
fi

echo "==> nvloom (localhost): extract ${CMAKE_TAR}"
tar xzf "$CMAKE_TAR"

if [[ ! -x "${CMAKE_ROOT}/bin/cmake" ]]; then
  echo "ERROR: after extract, expected ${CMAKE_ROOT}/bin/cmake (missing or not executable)." >&2
  exit 1
fi

echo "OK: CMake ${CMAKE_VER} at ${CMAKE_ROOT} (for shared workspace / aarch64 build hosts)"
