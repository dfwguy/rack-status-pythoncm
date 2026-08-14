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
# NVIS Rack Status — Utility. Mellanox HPC-X only (same tarball / unpack / hpcx_load as the former combined script).
# For optional NVIDIA CUDA toolkit and NCCL build, use install-cuda-nccl-workspace.sh (after HPC-X is present for NCCL).
#
# Usage: install-hpcx-workspace.sh [TOPDIR] [CUDA_MAJOR] [ARCH]
#   TOPDIR default: /home/cmsupport/workspace
#   CUDA_MAJOR: 12 | 13
#   ARCH: aarch64 only
set -euo pipefail

TOPDIR="${1:-/home/cmsupport/workspace}"
CUDA_MAJ="${2:-13}"
ARCH="${3:-aarch64}"

if [[ "$ARCH" != "aarch64" ]]; then
  echo "Only aarch64 is supported (matches installation/03-build-nccl.sh)." >&2
  exit 1
fi

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=workspace-cuda-major-common.sh
. "${_SCRIPT_DIR}/workspace-cuda-major-common.sh"

mkdir -p "$TOPDIR"
cd "$TOPDIR"
INIT="${TOPDIR}/${UNPACK}/hpcx-mt-init-ompi.sh"

if [[ ! -f "$INIT" ]]; then
  if command -v wget >/dev/null 2>&1; then
    wget -c -O "$TBZ" "$URL"
  elif command -v curl >/dev/null 2>&1; then
    curl -fL -o "$TBZ" "$URL"
  else
    echo "ERROR: need wget or curl in PATH" >&2
    exit 1
  fi
  tar xjvf "$TBZ"
fi

if [[ ! -f "$INIT" ]]; then
  echo "ERROR: missing after extract: $INIT" >&2
  exit 1
fi

echo "==> HPC-X (Mellanox — unpack + hpcx_load)"
set +u
# shellcheck disable=SC1090
source "$INIT"
hpcx_load
# shellcheck disable=SC1091
source /etc/profile || true

if [[ -f "${TOPDIR}/cuda/env.sh" ]]; then
  # shellcheck disable=SC1091
  source "${TOPDIR}/cuda/env.sh" || true
else
  echo "NOTE: ${TOPDIR}/cuda/env.sh not found (optional; use install-cuda-nccl-workspace.sh for CUDA toolkit)." >&2
fi

echo "OK: HPC-X at ${TOPDIR}/${UNPACK} (CUDA ${CUDA_MAJ}, ${ARCH})."
