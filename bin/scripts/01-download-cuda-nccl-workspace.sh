#!/usr/bin/env bash
# NVIS Rack Status — part 1 of CUDA/NCCL workspace install: run on the **dashboard host** (localhost).
# Downloads the NVIDIA CUDA .run installer into TOPDIR when NVIS_INSTALL_CUDA_TOOLKIT=1.
# After this succeeds, install-cuda-nccl-workspace.sh runs 02-install-cuda-nccl-workspace.sh on a compute node via pdsh.
#
# Usage: 01-download-cuda-nccl-workspace.sh [TOPDIR] [CUDA_MAJOR] [ARCH]
set -euo pipefail

TOPDIR="${1:-/home/cmsupport/workspace}"
CUDA_MAJ="${2:-13}"
ARCH="${3:-aarch64}"
INSTALL_CUDA="${NVIS_INSTALL_CUDA_TOOLKIT:-0}"

if [[ "$ARCH" != "aarch64" ]]; then
  echo "Only aarch64 is supported (matches installation/03-build-nccl.sh)." >&2
  exit 1
fi

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=workspace-cuda-major-common.sh
. "${_SCRIPT_DIR}/workspace-cuda-major-common.sh"

if [[ "$INSTALL_CUDA" != "1" ]]; then
  echo "OK: skip CUDA .run download (NVIS_INSTALL_CUDA_TOOLKIT is not 1)."
  exit 0
fi

echo "==> Download CUDA toolkit installer (.run) into ${TOPDIR}"
mkdir -p "$TOPDIR"
cd "$TOPDIR"
if [[ -f "${TOPDIR}/${RUN_FILE}" ]]; then
  echo "OK: ${RUN_FILE} already present."
  exit 0
fi
if command -v wget >/dev/null 2>&1; then
  wget -c -O "$RUN_FILE" "$RUN_URL"
elif command -v curl >/dev/null 2>&1; then
  curl -fL -o "$RUN_FILE" "$RUN_URL"
else
  echo "ERROR: need wget or curl" >&2
  exit 1
fi
echo "OK: downloaded ${RUN_FILE}"
