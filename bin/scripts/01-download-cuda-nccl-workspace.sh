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

# ---------------------------------------------------------------------------------------------
# NCCL / nccl-tests SOURCE FETCH — must happen HERE (dashboard/head node), not in phase 02.
#
# Phase 02 runs on a COMPUTE TRAY via pdsh, and the trays have NO ROUTE TO GITHUB (verified
# 2026-07-28: `git ls-remote` from ink50-dh01-h07-11 -> GITHUB_UNREACHABLE, while the head node
# reaches it fine). The original design cloned NCCL in phase 02, so the build created an empty
# nccl-<ver>/ directory and died at `git clone` — CUDA succeeded only because ITS download was
# already correctly placed in this phase.
#
# Clone + tag-checkout here; phase 02 then only compiles, which needs no network. Paths and the
# version->layout mapping MUST stay identical to 02-install-cuda-nccl-workspace.sh or the run
# reports "libnccl.so.2 not found" / "all_reduce_perf not found".
#   lib   -> <TOPDIR>/nccl[-<VER>]/nccl/build/lib
#   tests -> <TOPDIR>/nccl[-<VER>]/nccl-tests/build   (SIBLING of nccl/ for versioned builds)
# ---------------------------------------------------------------------------------------------
if [[ "${NVIS_BUILD_NCCL:-0}" == "1" ]]; then
  _nver="${NVIS_NCCL_VERSION:-2.28.9}"
  case "${_nver}" in
    system|default|2.28.9) _nroot="${TOPDIR}/nccl";               _troot="${TOPDIR}/nccl/nccl-tests" ;;
    *)                     _nroot="${TOPDIR}/nccl-${_nver}/nccl"; _troot="${TOPDIR}/nccl-${_nver}/nccl-tests" ;;
  esac

  echo "==> Fetch NCCL ${_nver} sources on $(hostname -s) (compute trays have no GitHub route)"
  mkdir -p "$(dirname "${_nroot}")"

  if [[ ! -d "${_nroot}/.git" ]]; then
    echo "==> git clone nccl -> ${_nroot}"
    git clone https://github.com/NVIDIA/nccl.git "${_nroot}"
  else
    echo "OK: nccl already cloned at ${_nroot}"
  fi

  # Resolve and check out the tag HERE while the network is available; phase 02 then finds it
  # locally. Upstream tags are v<X.Y.Z>-<n>; fail loudly listing real candidates.
  (
    cd "${_nroot}"
    git fetch --tags --force 2>/dev/null || true
    _tag=""
    for _c in "v${_nver}-1" "v${_nver}" "${_nver}"; do
      if git rev-parse --verify --quiet "refs/tags/${_c}" >/dev/null; then _tag="${_c}"; break; fi
    done
    if [[ -z "${_tag}" && "${_nver}" != "system" && "${_nver}" != "default" ]]; then
      echo "ERROR: no git tag for NCCL '${_nver}'. Nearby tags:" >&2
      git tag --list "v${_nver%%.*}.*" | tail -15 >&2
      exit 1
    fi
    if [[ -n "${_tag}" ]]; then
      echo "==> checkout ${_tag}"
      git checkout --quiet "${_tag}"
    fi
  )

  if [[ ! -d "${_troot}/.git" ]]; then
    echo "==> git clone nccl-tests -> ${_troot}"
    mkdir -p "$(dirname "${_troot}")"
    git clone https://github.com/NVIDIA/nccl-tests.git "${_troot}"
  else
    echo "OK: nccl-tests already cloned at ${_troot}"
  fi
  echo "OK: NCCL sources ready (nccl=${_nroot} tests=${_troot})"
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
