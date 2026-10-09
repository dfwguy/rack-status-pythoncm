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
# NVIS Rack Status — orchestrator: clone (01) and CMake download (00) on localhost only; nvloom build (02) on compute via pdsh.
# CMake tarball is always fetched/extracted on the dashboard host (shared TOPDIR). 02 only uses the unpacked tree + CUDA env.
# pdsh runs ``bash -c 'export …; exec bash 02 …'`` with explicit exports (CMAKE_CUDA_ARCHITECTURES=native, CMAKE_ROOT, CUDA_HOME, PATH, LD_LIBRARY_PATH, TOPDIR, CUDA_MAJ) so the target node does not rely on inherited empty env.
# Environment (set by Flask from the dashboard JSON):
#   NVIS_NVLOOM_BUILD_HOST — single hostname for pdsh -w (same TOPDIR on shared home)
#
# Usage: install-nvloom-workspace.sh [TOPDIR] [CUDA_MAJOR]
set -euo pipefail

# Workspace = the explicit argument, else the cluster's workspace (project setting
# connection.workspace_dir -> NVIS_WORKSPACE_DIR); workspace-dir-common.sh fails naming the setting.
WORKSPACE_DIR="${1:-}"
# shellcheck source=workspace-dir-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/workspace-dir-common.sh" || exit 2
CUDA_MAJ="${2:-13}"
HOST="${NVIS_NVLOOM_BUILD_HOST:-}"

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "$HOST" ]]; then
  echo "ERROR: NVIS_NVLOOM_BUILD_HOST must be set to one compute node hostname (pdsh target)." >&2
  exit 1
fi
if [[ "$HOST" == *","* ]] || [[ "$HOST" == *" "* ]]; then
  echo "ERROR: NVIS_NVLOOM_BUILD_HOST must be a single hostname (no commas or spaces)." >&2
  exit 1
fi

if ! command -v pdsh >/dev/null 2>&1; then
  echo "ERROR: pdsh not found in PATH on the dashboard host." >&2
  exit 1
fi

echo "==> Phase 1: git clone on localhost only ($(hostname 2>/dev/null || echo unknown))"
bash "${_SCRIPT_DIR}/01-clone-nvloom-workspace.sh" "$TOPDIR"

echo "==> Phase 1b: download/extract Kitware CMake on localhost only ($(hostname 2>/dev/null || echo unknown)) — ${HOST} not used"
bash "${_SCRIPT_DIR}/00-download-cmake-nvloom-workspace.sh" "$TOPDIR"

# pdsh often uses /bin/sh; pass explicit "export VAR=…;" so the remote shell does not lose env (same idea as install-cuda-nccl-workspace.sh).
# Keep CMAKE_VER in sync with 00-download-cmake-nvloom-workspace.sh / 02-build-nvloom-workspace.sh.
CMAKE_VER="3.28.1"
CMAKE_ROOT="${TOPDIR}/cmake-${CMAKE_VER}-linux-aarch64"

_script_q=$(printf '%q' "${_SCRIPT_DIR}/02-build-nvloom-workspace.sh")
_top_q=$(printf '%q' "$TOPDIR")
_maj_q=$(printf '%q' "$CUDA_MAJ")
_cuda_home_q=$(printf '%q' "${TOPDIR}/cuda${CUDA_MAJ}")
_cmake_root_q=$(printf '%q' "$CMAKE_ROOT")

_pds_inner=$(printf \
  'export TOPDIR=%s; export CUDA_MAJ=%s; export CUDA_HOME=%s; export CMAKE_ROOT=%s; export PATH=${CMAKE_ROOT}/bin:${CUDA_HOME}/bin:${PATH}; export LD_LIBRARY_PATH=${CUDA_HOME}/lib64:${LD_LIBRARY_PATH:-}; unset CMAKE_CUDA_ARCHITECTURES 2>/dev/null || true; export CMAKE_CUDA_ARCHITECTURES=native; exec bash %s %s %s' \
  "$_top_q" "$_maj_q" "$_cuda_home_q" "$_cmake_root_q" "$_script_q" "$_top_q" "$_maj_q")

_remote_quoted=$(printf '%q' "$_pds_inner")

echo "==> Phase 2: pdsh -w ${HOST} (nvloom build: uses workspace CMake from Phase 1b + cuda env)"
echo "==> remote: ${_pds_inner}"
# shellcheck disable=SC2029
pdsh -w "$HOST" bash -c "$_remote_quoted"
