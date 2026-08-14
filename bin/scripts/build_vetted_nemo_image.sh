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
# build_vetted_nemo_image.sh — bake the VETTED NeMo validation container for GB200/GB300 NVL72.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# WHY: the stock nemo-25.11.01 container ships NCCL 2.28.3, which SEGFAULTS at MNNVL init on GB200
# NVL72 ("NCCL INFO MNNVL busId ... healthMask 0xaa" -> SIGSEGV, srun exit 139) — even single-node,
# and for BOTH the NCCL pre-checks AND training (PyTorch dynamically loads the container's system
# libnccl.so.2, so there is exactly ONE NCCL to fix). The host NCCL 2.28.9 drives the SAME fabric
# fine (portable all_reduce ~914 GB/s; nvlink preflight PASS; torch CUDA in-container OK). Validated
# 2026-07-02 on bos01-b05 / bos01-b06 via LD_PRELOAD; this bakes the swap in so no runtime hack is
# needed and every customer cluster runs the SAME vetted image.
#
# WHAT: create a rootfs from the base .sqsh, then (1) replace /usr/lib/<arch>/libnccl.so.* with the
# vetted NCCL and (2) REBUILD the in-image nccl-tests (all_reduce/alltoall/all_gather _perf_mpi)
# against that SAME NCCL. Step 2 matters: the stock *_perf_mpi binaries are compiled against NCCL
# 2.28.3, so with a 2.28.9 lib the alltoall/allgather bootstrap ABI mismatches ("bootstrap.cc
# Message truncated: received 512 bytes instead of 256"). Rebuilding them against 2.28.9 makes the
# binaries, the runtime libnccl, and PyTorch's NCCL all EXACTLY the same version. Re-export a
# self-contained .sqsh (+ .sha256). HPC-X/UCX are NOT touched (UCX was only the crash reporter).
#
# RUN: on ONE arm64 build node with enroot (>=4.x) and ~2x image size scratch on the enroot data
# disk. Submit via sbatch (clean prolog) or run directly as the cmods user. GPUs NOT required.
#
# Usage: build_vetted_nemo_image.sh [BASE_SQSH] [OUT_SQSH] [VETTED_NCCL_LIB]
#   BASE_SQSH        stock image           (env NVIS_NEMO_BASE_SQSH;  default /raid/data/nemo-25.11.01.sqsh)
#   OUT_SQSH         vetted image out (NFS) (env NVIS_NEMO_VETTED_SQSH; default /home/cmods/workspace/images/nemo-vetted-nccl2.28.9.sqsh)
#   VETTED_NCCL_LIB  libnccl to bake in     (env NVIS_VETTED_NCCL_LIB;  default /home/cmods/workspace/nccl/bins/libnccl.so.2.28.9)
#   NVIS_CONTAINER_LIBDIR  in-image lib dir (default /usr/lib/aarch64-linux-gnu)
set -euo pipefail

BASE_SQSH="${1:-${NVIS_NEMO_BASE_SQSH:-/raid/data/nemo-25.11.01.sqsh}}"
OUT_SQSH="${2:-${NVIS_NEMO_VETTED_SQSH:-/home/cmods/workspace/images/nemo-vetted-nccl2.28.9.sqsh}}"
VETTED_NCCL_LIB="${3:-${NVIS_VETTED_NCCL_LIB:-/home/cmods/workspace/nccl/bins/libnccl.so.2.28.9}}"
ARCH_LIBDIR="${NVIS_CONTAINER_LIBDIR:-/usr/lib/aarch64-linux-gnu}"

# Inputs for rebuilding the in-image nccl-tests against the SAME NCCL (so *_perf_mpi binaries and
# the runtime libnccl match — fixes the alltoall/allgather bootstrap ABI mismatch). Set
# NVIS_REBUILD_NCCL_TESTS=0 to only swap the .so (legacy behavior).
REBUILD_NCCL_TESTS="${NVIS_REBUILD_NCCL_TESTS:-1}"
NCCL_BUILD_DIR="${NVIS_NCCL_BUILD_DIR:-/home/cmods/workspace/nccl/build}"        # 2.28.9 headers+lib (NCCL_HOME)
NCCL_TESTS_SRC="${NVIS_NCCL_TESTS_SRC:-/home/cmods/workspace/nccl/nccl-tests}"   # nccl-tests source tree
CONT_MPI_HOME="${NVIS_CONTAINER_MPI_HOME:-/opt/hpcx/ompi}"                       # in-image MPI (HPC-X OpenMPI)
CONT_CUDA_HOME="${NVIS_CONTAINER_CUDA_HOME:-/usr/local/cuda}"                    # in-image CUDA toolkit

command -v enroot >/dev/null || { echo "ERROR: enroot not found on this node"; exit 2; }
[ -f "$BASE_SQSH" ]       || { echo "ERROR: base image missing: $BASE_SQSH"; exit 2; }
[ -f "$VETTED_NCCL_LIB" ] || { echo "ERROR: vetted NCCL lib missing: $VETTED_NCCL_LIB"; exit 2; }
if [ "$REBUILD_NCCL_TESTS" = 1 ]; then
  [ -f "$NCCL_BUILD_DIR/include/nccl.h" ] || { echo "ERROR: NCCL headers missing: $NCCL_BUILD_DIR/include/nccl.h (set NVIS_NCCL_BUILD_DIR, or NVIS_REBUILD_NCCL_TESTS=0)"; exit 2; }
  [ -f "$NCCL_TESTS_SRC/Makefile" ]       || { echo "ERROR: nccl-tests source missing: $NCCL_TESTS_SRC/Makefile (set NVIS_NCCL_TESTS_SRC, or NVIS_REBUILD_NCCL_TESTS=0)"; exit 2; }
fi

NAME="nemovet-$$"
LIBVER="$(basename "$VETTED_NCCL_LIB")"                 # e.g. libnccl.so.2.28.9
VDIR="$(cd "$(dirname "$VETTED_NCCL_LIB")" && pwd)"     # host dir to bind-mount read-only
mkdir -p "$(dirname "$OUT_SQSH")"

cleanup() { enroot remove -f "$NAME" 2>/dev/null || true; }
trap cleanup EXIT

echo "== [1/4] create rootfs from $BASE_SQSH =="
enroot remove -f "$NAME" 2>/dev/null || true
enroot create --name "$NAME" "$BASE_SQSH"

echo "== [2/4] swap NCCL ($LIBVER) + rebuild nccl-tests inside rootfs (root in container ns) =="
# Mounts: host-nccl = vetted .so; host-nccl-build = 2.28.9 headers+lib (NCCL_HOME for the rebuild);
# host-nccl-tests = nccl-tests source. The in-container script is piped via `bash -s` (no quoting
# hell). Outer-shell vars are expanded by the unquoted heredoc; container-local vars are escaped.
_mounts=( --mount "${VDIR}:/host-nccl" )
if [ "$REBUILD_NCCL_TESTS" = 1 ]; then
  _mounts+=( --mount "${NCCL_BUILD_DIR}:/host-nccl-build" --mount "${NCCL_TESTS_SRC}:/host-nccl-tests" )
fi
enroot start --root --rw "${_mounts[@]}" "$NAME" bash -s <<INEOF
set -e
# (1) swap libnccl.so* -> vetted ${LIBVER}
cd "${ARCH_LIBDIR}"
echo '-- libnccl before --'; ls -l libnccl.so* 2>/dev/null || echo '(none)'
rm -f libnccl.so.2.* libnccl.so.2 libnccl.so
cp -f "/host-nccl/${LIBVER}" "./${LIBVER}"
ln -sf "${LIBVER}" libnccl.so.2
ln -sf "${LIBVER}" libnccl.so
chmod 0644 "./${LIBVER}"
ldconfig 2>/dev/null || true
echo '-- libnccl after --'; ls -l libnccl.so*

# (2) rebuild nccl-tests (MPI=1) against the SAME NCCL so *_perf_mpi match the runtime lib.
if [ "${REBUILD_NCCL_TESTS}" = 1 ]; then
  echo '-- rebuilding nccl-tests (MPI=1) against ${LIBVER} --'
  command -v "${CONT_CUDA_HOME}/bin/nvcc" >/dev/null 2>&1 || { echo "ERROR: nvcc not at ${CONT_CUDA_HOME}/bin (set NVIS_CONTAINER_CUDA_HOME)"; exit 3; }
  [ -x "${CONT_MPI_HOME}/bin/mpicxx" ] || { echo "ERROR: mpicxx not at ${CONT_MPI_HOME}/bin (set NVIS_CONTAINER_MPI_HOME)"; exit 3; }
  rm -rf /tmp/nccl-tests
  cp -a /host-nccl-tests /tmp/nccl-tests
  make -C /tmp/nccl-tests clean >/dev/null 2>&1 || true
  make -C /tmp/nccl-tests MPI=1 MPI_HOME="${CONT_MPI_HOME}" CUDA_HOME="${CONT_CUDA_HOME}" NCCL_HOME=/host-nccl-build -j"\$(nproc)"
  # Install every built *_perf as /usr/local/bin/<name>_mpi (what the NeMo job invokes).
  _n=0
  for _b in /tmp/nccl-tests/build/*_perf; do
    [ -x "\$_b" ] || continue
    cp -f "\$_b" "/usr/local/bin/\$(basename "\$_b")_mpi"
    _n=\$((_n+1))
  done
  echo "-- installed \$_n *_perf_mpi binaries (built vs ${LIBVER}) --"
  ls -l /usr/local/bin/all_reduce_perf_mpi /usr/local/bin/alltoall_perf_mpi /usr/local/bin/all_gather_perf_mpi 2>/dev/null || true
  rm -rf /tmp/nccl-tests
else
  echo '-- NVIS_REBUILD_NCCL_TESTS=0: skipping nccl-tests rebuild (only .so swapped) --'
fi
INEOF

echo "== [3/4] export -> $OUT_SQSH =="
rm -f "$OUT_SQSH" "${OUT_SQSH}.sha256"
enroot export --output "$OUT_SQSH" "$NAME"

echo "== [4/4] checksum =="
sha256sum "$OUT_SQSH" | tee "${OUT_SQSH}.sha256"
ls -lh "$OUT_SQSH"
echo "DONE: vetted NeMo image -> $OUT_SQSH"
echo
echo "This image has libnccl + nccl-tests (*_perf_mpi) + PyTorch NCCL all at ${LIBVER} — no LD_PRELOAD needed."
echo "Run the NeMo x-rack job against it with:"
echo "  NEMO_SQSH_SRC=$OUT_SQSH \\"
echo "  NVIS_NCCL_PRELOAD_DISABLE=1 \\"
echo "  run-nemo-xrack.sh '<rack specs>'"
echo "(NVIS_NCCL_PRELOAD_DISABLE=1 uses the image's own vetted NCCL; the alltoall/allgather"
echo " bootstrap mismatch is gone, so the full NVIS_NCCL_PREFLIGHT set can run.)"
