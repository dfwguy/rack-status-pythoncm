#!/bin/bash
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
# WHAT: create a rootfs from the base .sqsh, replace /usr/lib/<arch>/libnccl.so.* with the vetted
# NCCL, re-export a self-contained .sqsh (+ .sha256). HPC-X/UCX are NOT touched (UCX was only the
# crash reporter, not the cause).
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

command -v enroot >/dev/null || { echo "ERROR: enroot not found on this node"; exit 2; }
[ -f "$BASE_SQSH" ]       || { echo "ERROR: base image missing: $BASE_SQSH"; exit 2; }
[ -f "$VETTED_NCCL_LIB" ] || { echo "ERROR: vetted NCCL lib missing: $VETTED_NCCL_LIB"; exit 2; }

NAME="nemovet-$$"
LIBVER="$(basename "$VETTED_NCCL_LIB")"                 # e.g. libnccl.so.2.28.9
VDIR="$(cd "$(dirname "$VETTED_NCCL_LIB")" && pwd)"     # host dir to bind-mount read-only
mkdir -p "$(dirname "$OUT_SQSH")"

cleanup() { enroot remove -f "$NAME" 2>/dev/null || true; }
trap cleanup EXIT

echo "== [1/4] create rootfs from $BASE_SQSH =="
enroot remove -f "$NAME" 2>/dev/null || true
enroot create --name "$NAME" "$BASE_SQSH"

echo "== [2/4] swap NCCL ($LIBVER) inside rootfs (as root in the container namespace) =="
enroot start --root --rw --mount "${VDIR}:/host-nccl" "$NAME" bash -c "
  set -e
  cd '${ARCH_LIBDIR}'
  echo '-- before --'; ls -l libnccl.so* 2>/dev/null || echo '(none)'
  rm -f libnccl.so.2.* libnccl.so.2 libnccl.so
  cp -f '/host-nccl/${LIBVER}' './${LIBVER}'
  ln -sf '${LIBVER}' libnccl.so.2
  ln -sf '${LIBVER}' libnccl.so
  chmod 0644 './${LIBVER}'
  ldconfig 2>/dev/null || true
  echo '-- after --'; ls -l libnccl.so*
"

echo "== [3/4] export -> $OUT_SQSH =="
rm -f "$OUT_SQSH" "${OUT_SQSH}.sha256"
enroot export --output "$OUT_SQSH" "$NAME"

echo "== [4/4] checksum =="
sha256sum "$OUT_SQSH" | tee "${OUT_SQSH}.sha256"
ls -lh "$OUT_SQSH"
echo "DONE: vetted NeMo image -> $OUT_SQSH"
echo "Point the NEMO job at it via NEMO_SQSH_SRC=$OUT_SQSH (staged to /raid/data by run_singlerack.sh)."
