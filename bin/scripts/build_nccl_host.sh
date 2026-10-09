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
# build_nccl_host.sh — build a vetted NCCL (+ matched nccl-tests) and publish it on NFS.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Produces the host libnccl.so.2 that the direct NCCL microbenchmark loads at runtime via
# LD_LIBRARY_PATH (see nccl_allreduce.sbatch's version selector). Because /home is NFS-
# shared, publishing once here makes it visible on every compute node.
#
# WHY build local, then copy: this cluster's NFS export rejects install(1)-style chmod
# ("install: setting permissions ... Operation not permitted") even though plain chmod/cp
# work. NCCL's Makefile uses install, so we build into node-local /tmp (BUILDDIR) where
# install succeeds, then publish the artifacts to NFS with plain cp (mode is set at
# create-time, so no post-copy chmod is attempted).
#
# Layout convention (matches nccl_allreduce.sbatch's version glob):
#   <ws>/nccl-<VER>/nccl        — NCCL source (git clone at the version tag)
#   <ws>/nccl-<VER>/nccl-tests  — nccl-tests source
#   -> publishes <ws>/nccl-<VER>/nccl/build/lib/libnccl.so.2  (+ nccl-tests/build/*_perf)
#
# Usage:   build_nccl_host.sh [VERSION]      (default 2.30.7)
# Env:     NVIS_WS (default: the cluster's workspace, NVIS_WORKSPACE_DIR), CUDA_HOME, HPCX_DIR,
#          NVIS_NVCC_GENCODE (default sm_100 + PTX for GB200/GB300), JOBS (default nproc),
#          NVIS_LOCAL_BUILD (node-local build dir, default /tmp/nccl-build-<VER>-<pid>).
set -euo pipefail

VER="${1:-2.30.7}"
# The cluster's workspace (project setting -> NVIS_WORKSPACE_DIR); NVIS_WS still overrides.
WORKSPACE_DIR="${NVIS_WS:-}"
# shellcheck source=workspace-dir-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/workspace-dir-common.sh" || exit 2
WS="${WORKSPACE_DIR}"
SRC="${WS}/nccl-${VER}"
JOBS="${JOBS:-$(nproc)}"
CUDA_HOME="${CUDA_HOME:-$(ls -d "${WS}"/cuda13 "${WS}"/cuda 2>/dev/null | head -1)}"
HPCX_DIR="${HPCX_DIR:-$(ls -d "${WS}"/hpcx-*cuda13*aarch64 2>/dev/null | grep -v '\.tbz$' | head -1)}"
# GB200/GB300 are Blackwell datacenter (compute capability 10.0 = sm_100). Emit sm_100 SASS
# plus compute_100 PTX for forward-compat. Override for other targets via NVIS_NVCC_GENCODE.
GENCODE="${NVIS_NVCC_GENCODE:--gencode=arch=compute_100,code=sm_100 -gencode=arch=compute_100,code=compute_100}"
LOCAL="${NVIS_LOCAL_BUILD:-${TMPDIR:-/tmp}/nccl-build-${VER}-$$}"

export PATH="${CUDA_HOME}/bin:${PATH}"

echo "build_nccl_host: ver=${VER} src=${SRC}"
echo "build_nccl_host: cuda=${CUDA_HOME} hpcx=${HPCX_DIR} jobs=${JOBS}"
echo "build_nccl_host: gencode=${GENCODE}"
echo "build_nccl_host: local build dir=${LOCAL}"
[ -x "${CUDA_HOME}/bin/nvcc" ] || { echo "build_nccl_host: nvcc not found under ${CUDA_HOME}"; exit 2; }
command -v g++ >/dev/null 2>&1 || { echo "build_nccl_host: g++ not on PATH (need a host C++ compiler on this node)"; exit 2; }
[ -d "${SRC}/nccl" ] || { echo "build_nccl_host: no NCCL source at ${SRC}/nccl"; exit 2; }

rm -rf "${LOCAL}"; mkdir -p "${LOCAL}"
trap 'rm -rf "${LOCAL}"' EXIT

# 1) libnccl.so.2 — build into node-local BUILDDIR (install works on local fs).
echo "build_nccl_host: building libnccl into ${LOCAL}/nccl (10-20 min)…"
make -C "${SRC}/nccl" -j"${JOBS}" src.build \
  BUILDDIR="${LOCAL}/nccl" CUDA_HOME="${CUDA_HOME}" NVCC_GENCODE="${GENCODE}"

# Publish libnccl to the NFS convention path with plain cp (-d keeps the .so symlink chain;
# no -p, so no install-style chmod is attempted on NFS).
DEST_NCCL="${SRC}/nccl/build"
echo "build_nccl_host: publishing libnccl -> ${DEST_NCCL}/lib"
rm -rf "${DEST_NCCL}" 2>/dev/null || true   # drop any partials from earlier failed runs
mkdir -p "${DEST_NCCL}/lib" "${DEST_NCCL}/include"
cp -d "${LOCAL}/nccl/lib/"libnccl.so* "${DEST_NCCL}/lib/"
cp -r "${LOCAL}/nccl/include/." "${DEST_NCCL}/include/" 2>/dev/null || true

# 2) nccl-tests (best effort) — a fully version-matched binary the sbatch can prefer. The
#    direct test also works with the system nccl-tests (ABI-compatible), so this is optional.
DEST_TESTS="${SRC}/nccl-tests/build"
if [ -d "${SRC}/nccl-tests" ] && [ -n "${HPCX_DIR}" ] && [ -d "${HPCX_DIR}/ompi" ]; then
  echo "build_nccl_host: building nccl-tests (MPI=1) into ${LOCAL}/nccl-tests…"
  if make -C "${SRC}/nccl-tests" -j"${JOBS}" MPI=1 MPI_HOME="${HPCX_DIR}/ompi" \
       CUDA_HOME="${CUDA_HOME}" NCCL_HOME="${LOCAL}/nccl" BUILDDIR="${LOCAL}/nccl-tests"; then
    echo "build_nccl_host: publishing nccl-tests -> ${DEST_TESTS}"
    rm -rf "${DEST_TESTS}" 2>/dev/null || true
    mkdir -p "${DEST_TESTS}"
    find "${LOCAL}/nccl-tests" -maxdepth 2 -type f -name '*_perf' -exec cp {} "${DEST_TESTS}/" \;
  else
    echo "build_nccl_host: nccl-tests build failed (non-fatal — sbatch falls back to the system binary)"
  fi
else
  echo "build_nccl_host: skipping nccl-tests (no source or HPC-X ompi) — sbatch uses the system binary"
fi

LIB="${DEST_NCCL}/lib/libnccl.so.2"
if [ -e "${LIB}" ]; then
  _v="$(strings "$(readlink -f "${LIB}")" 2>/dev/null | grep -oE '^2\.[0-9]+\.[0-9]+' | head -1)"
  echo "build_nccl_host: DONE — published ${LIB} (version ${_v:-?})"
  ls -la "${DEST_NCCL}/lib/"libnccl.so*
else
  echo "build_nccl_host: ERROR — libnccl.so.2 not published at ${LIB}"; exit 1
fi
