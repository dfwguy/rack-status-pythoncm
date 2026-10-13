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
# Allocation-scoped container cache gate for validation presets.
# Direct SSH/pdsh to idle compute nodes is forbidden on some sites, so every read,
# optional rsync, atomic promotion, and container device check runs inside Slurm.
#SBATCH --job-name=nvis-container-stage
#SBATCH --exclusive
#SBATCH --time=01:30:00

set -euo pipefail

: "${NVIS_STAGE_SOURCE:?NVIS_STAGE_SOURCE is required}"
: "${NVIS_STAGE_DEST:?NVIS_STAGE_DEST is required}"
: "${NVIS_STAGE_RSYNC:=0}"
: "${NVIS_STAGE_CONTAINER_PREFLIGHT:=0}"
: "${NVIS_STAGE_EXPECTED_GPUS:=0}"
: "${NVIS_STAGE_EXPECTED_NCCL:=0}"
: "${NVIS_STAGE_EXPECTED_CUDA:=}"
: "${NVIS_STAGE_EXPECTED_SIZE:?NVIS_STAGE_EXPECTED_SIZE is required}"
: "${NVIS_STAGE_EXPECTED_SHA256:?NVIS_STAGE_EXPECTED_SHA256 is required}"
: "${NVIS_SCRIPTS_DIR:=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

nodes="$(scontrol show hostnames "${SLURM_JOB_NODELIST}" | grep -c .)"
(( nodes > 0 )) || { echo "ERROR: allocation contains no nodes" >&2; exit 2; }

export NVIS_STAGE_SOURCE NVIS_STAGE_DEST NVIS_STAGE_RSYNC
export NVIS_STAGE_EXPECTED_SIZE NVIS_STAGE_EXPECTED_SHA256
export NVIS_STAGE_EXPECTED_GPUS NVIS_STAGE_EXPECTED_NCCL NVIS_STAGE_EXPECTED_CUDA
srun --kill-on-bad-exit=1 --nodes="${nodes}" --ntasks="${nodes}" --ntasks-per-node=1 \
  bash -euo pipefail -c '
    source_path="$NVIS_STAGE_SOURCE"
    destination="$NVIS_STAGE_DEST"
    # The node cache is checked first and the source only when a copy is needed: where P2P fills
    # the cache (rsync off) the source need not be visible from the node at all -- on Nscale NC
    # /cluster/containers lives on the app server, and checking it first failed a 48-node NeMo preset
    # launch (Slurm 192718) in two seconds while every node held the image P2P had verified.
    mkdir -p "$(dirname "$destination")"
    lock="${destination}.nvis-stage.lock"
    exec 9>"$lock"
    flock -x 9
    destination_size="$(stat -c %s "$destination" 2>/dev/null || true)"
    if [[ "$destination_size" == "$NVIS_STAGE_EXPECTED_SIZE" && -r "$destination" ]]; then
      destination_sha="$(sha256sum "$destination" | awk "{print \$1}")"
      if [[ "$destination_sha" == "$NVIS_STAGE_EXPECTED_SHA256" ]]; then
        echo "IMAGE_STAGE_READY $(hostname): bytes=$destination_size sha256=$destination_sha cached=yes path=$destination"
        exit 0
      fi
      echo "IMAGE_STAGE_CORRUPT $(hostname): same-size sha256=$destination_sha expected=$NVIS_STAGE_EXPECTED_SHA256" >&2
    fi
    if [[ "$NVIS_STAGE_RSYNC" != 1 || "$source_path" == "$destination" ]]; then
      echo "IMAGE_STAGE_FAIL $(hostname): expected=$NVIS_STAGE_EXPECTED_SIZE actual=${destination_size:-missing} path=$destination rsync=$NVIS_STAGE_RSYNC" >&2
      exit 3
    fi
    source_size="$(stat -c %s "$source_path" 2>/dev/null || true)"
    [[ "$source_size" == "$NVIS_STAGE_EXPECTED_SIZE" ]] || {
      echo "IMAGE_STAGE_FAIL $(hostname): source size expected=$NVIS_STAGE_EXPECTED_SIZE actual=${source_size:-missing}: $source_path" >&2
      exit 2
    }
    partial="${destination}.partial"
    if [[ -s "$partial" ]]; then
      # Preserve useful work after an interrupted stage and verify the overlap.
      rsync -a --partial --append-verify -- "$source_path" "$partial"
    else
      # Squashfs is already compressed. A fresh whole-file transfer avoids the
      # delta algorithm extra source/destination reads and CPU cost.
      rsync -a --whole-file --partial -- "$source_path" "$partial"
    fi
    partial_size="$(stat -c %s "$partial" 2>/dev/null || true)"
    if [[ "$partial_size" != "$NVIS_STAGE_EXPECTED_SIZE" ]]; then
      echo "IMAGE_STAGE_FAIL $(hostname): partial expected=$NVIS_STAGE_EXPECTED_SIZE actual=${partial_size:-missing}" >&2
      exit 4
    fi
    partial_sha="$(sha256sum "$partial" | awk "{print \$1}")"
    if [[ "$partial_sha" != "$NVIS_STAGE_EXPECTED_SHA256" ]]; then
      echo "IMAGE_STAGE_FAIL $(hostname): partial sha256=$partial_sha expected=$NVIS_STAGE_EXPECTED_SHA256" >&2
      exit 5
    fi
    mv -- "$partial" "$destination"
    echo "IMAGE_STAGE_READY $(hostname): bytes=$partial_size sha256=$partial_sha cached=no path=$destination"
  '

if [[ "${NVIS_STAGE_CONTAINER_PREFLIGHT}" == 1 ]]; then
  export MELLANOX_VISIBLE_DEVICES=all
  srun --kill-on-bad-exit=1 --nodes="${nodes}" --ntasks="${nodes}" --ntasks-per-node=1 \
    --container-image="${NVIS_STAGE_DEST}" --container-writable --no-container-mount-home \
    --container-mounts=/dev/infiniband:/dev/infiniband \
    --container-env=MELLANOX_VISIBLE_DEVICES,NVIS_STAGE_DEST,NVIS_STAGE_EXPECTED_GPUS,NVIS_STAGE_EXPECTED_NCCL,NVIS_STAGE_EXPECTED_CUDA \
    bash -euo pipefail -c '
      test -d /dev/infiniband
      test -e /dev/infiniband/rdma_cm
      test -d /sys/class/infiniband
      count=$(find /dev/infiniband -maxdepth 1 -name "uverbs*" -type c 2>/dev/null | wc -l)
      (( count > 0 ))
      gpus=$(nvidia-smi -L 2>/dev/null | grep -c "^GPU")
      if (( NVIS_STAGE_EXPECTED_GPUS > 0 )); then
        [[ "$gpus" == "$NVIS_STAGE_EXPECTED_GPUS" ]] || {
          echo "CONTAINER_PREFLIGHT_FAIL $(hostname): gpus=$gpus expected=$NVIS_STAGE_EXPECTED_GPUS" >&2; exit 15;
        }
      fi
      if (( NVIS_STAGE_EXPECTED_NCCL > 0 )); then
        nccl=$(python3 - <<"PY"
import ctypes
version = ctypes.c_int()
lib = ctypes.CDLL("libnccl.so.2")
assert lib.ncclGetVersion(ctypes.byref(version)) == 0
print(version.value)
PY
)
        [[ "$nccl" == "$NVIS_STAGE_EXPECTED_NCCL" ]] || {
          echo "CONTAINER_PREFLIGHT_FAIL $(hostname): nccl=$nccl expected=$NVIS_STAGE_EXPECTED_NCCL" >&2; exit 16;
        }
      else nccl=unchecked; fi
      if [[ -n "$NVIS_STAGE_EXPECTED_CUDA" ]]; then
        cuda=$(nvcc --version | sed -n "s/.*release \([0-9][0-9.]*\).*/\1/p" | head -1)
        [[ "$cuda" == "$NVIS_STAGE_EXPECTED_CUDA" ]] || {
          echo "CONTAINER_PREFLIGHT_FAIL $(hostname): cuda=$cuda expected=$NVIS_STAGE_EXPECTED_CUDA" >&2; exit 17;
        }
      else cuda=unchecked; fi
      echo "CONTAINER_IB_READY $(hostname): uverbs=$count gpus=$gpus nccl=$nccl cuda=$cuda image=$NVIS_STAGE_DEST"
    '
fi

echo "VALIDATION_CONTAINER_STAGE_COMPLETE nodes=${nodes} destination=${NVIS_STAGE_DEST}"
