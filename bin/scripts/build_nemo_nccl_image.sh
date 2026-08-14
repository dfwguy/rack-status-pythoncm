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
# build_nemo_nccl_image.sh — bake a NeMo validation container with a specific NCCL version built FROM SOURCE.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# WHY: NCCL 2.28.9 trips a cross-rack alltoall bootstrap bug on multi-domain NVL72
# ("bootstrap.cc Message truncated: received 512 bytes instead of 256"). An A/B sweep proved NO env
# knob (PXN/PROTO/NCHANNELS) fixes it — it's a library bug, not tuning. This bakes a DIFFERENT NCCL
# version (e.g. 2.30.7) to test/ship a native fix. Unlike build_vetted_nemo_image.sh (which swaps a
# PREBUILT host .so), this builds NCCL FROM SOURCE inside the container using the container's own
# CUDA/MPI toolchain — so no host nvcc is needed and the lib, nccl-tests (*_perf_mpi), and PyTorch's
# NCCL are all the SAME freshly built version. No LD_PRELOAD, no ABI mismatch.
#
# WHAT: create a rootfs from the base .sqsh, then INSIDE it: (1) `make src.build` the mounted NCCL
# source, (2) install the built libnccl.so.* into the container libdir + ldconfig, (3) rebuild the
# mounted nccl-tests (MPI=1) against that same NCCL_HOME and install *_perf_mpi to /usr/local/bin,
# (4) export a self-contained, version-named .sqsh (+ .sha256). GPUs NOT required to build.
#
# RUN: on ONE arm64 node with enroot (>=4.x) + ~2x image scratch on the enroot data disk. The head
# node works (aarch64, has enroot). NCCL/nccl-tests sources must already be checked out on the host.
#
# Usage: build_nemo_nccl_image.sh [NCCL_SRC] [NCCL_TESTS_SRC] [BASE_SQSH] [OUT_SQSH]
#   NCCL_SRC        NCCL git tree (checked out to target tag)
#                   (env NVIS_NCCL_SRC;        default /home/cmods/workspace/nccl-2.30.7/nccl)
#   NCCL_TESTS_SRC  nccl-tests git tree
#                   (env NVIS_NCCL_TESTS_SRC;  default /home/cmods/workspace/nccl-2.30.7/nccl-tests)
#   BASE_SQSH       stock base image
#                   (env NVIS_NEMO_BASE_SQSH;  default /home/cmods/nemo-25.11.01.sqsh)
#   OUT_SQSH        output image (name it by NCCL version, e.g. nemo-nccl2.30.7.sqsh)
#                   (env NVIS_NEMO_OUT_SQSH;   default /home/cmods/workspace/images/nemo-nccl2.30.7.sqsh)
#   NVIS_CONTAINER_LIBDIR    in-image lib dir  (default /usr/lib/aarch64-linux-gnu)
#   NVIS_CONTAINER_MPI_HOME  in-image MPI      (default /opt/hpcx/ompi)
#   NVIS_CONTAINER_CUDA_HOME in-image CUDA     (default /usr/local/cuda)
set -euo pipefail

NCCL_SRC="${1:-${NVIS_NCCL_SRC:-/home/cmods/workspace/nccl-2.30.7/nccl}}"
NCCL_TESTS_SRC="${2:-${NVIS_NCCL_TESTS_SRC:-/home/cmods/workspace/nccl-2.30.7/nccl-tests}}"
BASE_SQSH="${3:-${NVIS_NEMO_BASE_SQSH:-/home/cmods/nemo-25.11.01.sqsh}}"
OUT_SQSH="${4:-${NVIS_NEMO_OUT_SQSH:-/home/cmods/workspace/images/nemo-nccl2.30.7.sqsh}}"
ARCH_LIBDIR="${NVIS_CONTAINER_LIBDIR:-/usr/lib/aarch64-linux-gnu}"
CONT_MPI_HOME="${NVIS_CONTAINER_MPI_HOME:-/opt/hpcx/ompi}"
CONT_CUDA_HOME="${NVIS_CONTAINER_CUDA_HOME:-/usr/local/cuda}"

command -v enroot >/dev/null || { echo "ERROR: enroot not found on this node"; exit 2; }
[ -f "$BASE_SQSH" ]            || { echo "ERROR: base image missing: $BASE_SQSH"; exit 2; }
[ -f "$NCCL_SRC/Makefile" ]    || { echo "ERROR: NCCL source missing: $NCCL_SRC/Makefile (set NVIS_NCCL_SRC)"; exit 2; }
[ -f "$NCCL_TESTS_SRC/Makefile" ] || { echo "ERROR: nccl-tests source missing: $NCCL_TESTS_SRC/Makefile (set NVIS_NCCL_TESTS_SRC)"; exit 2; }

NAME="nemonccl-$$"
NSRC="$(cd "$NCCL_SRC" && pwd)"
TSRC="$(cd "$NCCL_TESTS_SRC" && pwd)"
mkdir -p "$(dirname "$OUT_SQSH")"

cleanup() { enroot remove -f "$NAME" 2>/dev/null || true; }
trap cleanup EXIT

echo "== [1/4] create rootfs from $BASE_SQSH =="
enroot remove -f "$NAME" 2>/dev/null || true
enroot create --name "$NAME" "$BASE_SQSH"

echo "== [2/4] build NCCL + nccl-tests from source inside rootfs (root in container ns) =="
# Mounts: host-nccl-src / host-nccl-tests are the checked-out source trees (read-only bind); the
# build happens on writable COPIES in /tmp so the host trees stay pristine. Outer-shell vars are
# expanded by the unquoted heredoc; container-local vars are escaped (\$).
# NVIDIA_VISIBLE_DEVICES=void disables enroot's 98-nvidia.sh GPU-injection hook: building NCCL needs
# only nvcc (compiler), NOT a GPU or nvidia-container-cli — so this lets the build run on a plain
# build node (e.g. the head) that has no GPU/driver. At RUNTIME the GPU nodes inject GPUs normally.
export NVIDIA_VISIBLE_DEVICES=void
enroot start --root --rw \
  --mount "${NSRC}:/host-nccl-src" \
  --mount "${TSRC}:/host-nccl-tests" \
  "$NAME" bash -s <<INEOF
set -e
export CUDA_HOME="${CONT_CUDA_HOME}"
export PATH="\${CUDA_HOME}/bin:\${PATH}"
command -v "\${CUDA_HOME}/bin/nvcc" >/dev/null 2>&1 || { echo "ERROR: nvcc not at ${CONT_CUDA_HOME}/bin (set NVIS_CONTAINER_CUDA_HOME)"; exit 3; }
[ -x "${CONT_MPI_HOME}/bin/mpicxx" ] || { echo "ERROR: mpicxx not at ${CONT_MPI_HOME}/bin (set NVIS_CONTAINER_MPI_HOME)"; exit 3; }
echo "-- nvcc: \$(\${CUDA_HOME}/bin/nvcc --version | grep -o 'release [0-9.]*') --"

# (1) build NCCL from source on a writable copy.
rm -rf /tmp/nccl-src
cp -a /host-nccl-src /tmp/nccl-src
echo "-- building NCCL (make -j src.build) --"
make -C /tmp/nccl-src -j"\$(nproc)" src.build CUDA_HOME="\${CUDA_HOME}"
NCCL_BUILD=/tmp/nccl-src/build
LIB="\$(ls \${NCCL_BUILD}/lib/libnccl.so.2.* 2>/dev/null | grep -E 'so\.[0-9]+\.[0-9]+\.[0-9]+\$' | head -1)"
[ -n "\$LIB" ] || { echo "ERROR: NCCL build produced no libnccl.so.2.x.y.z"; ls -l \${NCCL_BUILD}/lib || true; exit 4; }
LIBVER="\$(basename "\$LIB")"
echo "-- built \$LIBVER --"

# (2) install built libnccl into container libdir.
cd "${ARCH_LIBDIR}"
echo '-- libnccl before --'; ls -l libnccl.so* 2>/dev/null || echo '(none)'
rm -f libnccl.so.2.* libnccl.so.2 libnccl.so
cp -f "\$LIB" "./\$LIBVER"
ln -sf "\$LIBVER" libnccl.so.2
ln -sf "\$LIBVER" libnccl.so
chmod 0644 "./\$LIBVER"
ldconfig 2>/dev/null || true
echo '-- libnccl after --'; ls -l libnccl.so*

# (3) rebuild nccl-tests (MPI=1) against the freshly built NCCL so *_perf_mpi match the runtime lib.
rm -rf /tmp/nccl-tests
cp -a /host-nccl-tests /tmp/nccl-tests
make -C /tmp/nccl-tests clean >/dev/null 2>&1 || true
echo "-- building nccl-tests (MPI=1) against \$LIBVER --"
make -C /tmp/nccl-tests MPI=1 MPI_HOME="${CONT_MPI_HOME}" CUDA_HOME="\${CUDA_HOME}" NCCL_HOME="\${NCCL_BUILD}" -j"\$(nproc)"
_n=0
for _b in /tmp/nccl-tests/build/*_perf; do
  [ -x "\$_b" ] || continue
  cp -f "\$_b" "/usr/local/bin/\$(basename "\$_b")_mpi"
  _n=\$((_n+1))
done
echo "-- installed \$_n *_perf_mpi binaries (built vs \$LIBVER) --"
ls -l /usr/local/bin/all_reduce_perf_mpi /usr/local/bin/alltoall_perf_mpi /usr/local/bin/all_gather_perf_mpi 2>/dev/null || true
rm -rf /tmp/nccl-src /tmp/nccl-tests
INEOF

echo "== [3/4] export -> $OUT_SQSH =="
rm -f "$OUT_SQSH" "${OUT_SQSH}.sha256"
enroot export --output "$OUT_SQSH" "$NAME"

echo "== [4/4] checksum =="
sha256sum "$OUT_SQSH" | tee "${OUT_SQSH}.sha256"
ls -lh "$OUT_SQSH"
echo "DONE: NeMo image with source-built NCCL -> $OUT_SQSH"
echo
echo "This image has libnccl + nccl-tests (*_perf_mpi) + PyTorch NCCL all at the SAME source-built"
echo "version — no LD_PRELOAD needed. Run the NeMo x-rack job against it with:"
echo "  NEMO_SQSH_SRC=$OUT_SQSH NVIS_NCCL_PRELOAD_DISABLE=1 run-nemo-xrack.sh '<rack specs>'"
