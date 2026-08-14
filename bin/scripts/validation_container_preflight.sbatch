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
# Allocation-scoped container device/runtime visibility gate for shared NFS images.
#SBATCH --exclusive
#SBATCH --time=00:20:00
set -euo pipefail
: "${NVIS_PREFLIGHT_IMAGE:?}"
: "${NVIS_PREFLIGHT_EXPECTED_NCCL:=0}"
: "${NVIS_PREFLIGHT_EXPECTED_CUDA:=}"
nodes="$(scontrol show hostnames "$SLURM_JOB_NODELIST" | grep -c .)"
export MELLANOX_VISIBLE_DEVICES=all
export NVIS_PREFLIGHT_EXPECTED_NCCL NVIS_PREFLIGHT_EXPECTED_CUDA
srun --nodes="$nodes" --ntasks="$nodes" --ntasks-per-node=1 --kill-on-bad-exit=1 --label \
  --container-image="$NVIS_PREFLIGHT_IMAGE" --container-writable --no-container-mount-home \
  --container-mounts=/dev/infiniband:/dev/infiniband \
  --container-env=MELLANOX_VISIBLE_DEVICES,NVIS_PREFLIGHT_EXPECTED_NCCL,NVIS_PREFLIGHT_EXPECTED_CUDA \
  bash -euo pipefail -c '
    test -e /dev/infiniband/rdma_cm
    test -d /sys/class/infiniband
    uverbs=$(find /dev/infiniband -maxdepth 1 -name "uverbs*" -type c 2>/dev/null | wc -l)
    (( uverbs > 0 ))
    nccl=$(python3 - <<"PY"
import ctypes
version = ctypes.c_int()
lib = ctypes.CDLL("libnccl.so.2")
assert lib.ncclGetVersion(ctypes.byref(version)) == 0
print(version.value)
PY
)
    [[ "$NVIS_PREFLIGHT_EXPECTED_NCCL" == 0 || "$nccl" == "$NVIS_PREFLIGHT_EXPECTED_NCCL" ]]
    cuda=$(nvcc --version | sed -n "s/.*release \([0-9][0-9.]*\).*/\1/p" | head -1)
    [[ -z "$NVIS_PREFLIGHT_EXPECTED_CUDA" || "$cuda" == "$NVIS_PREFLIGHT_EXPECTED_CUDA" ]]
    echo "CONTAINER_IB_READY $(hostname): uverbs=$uverbs nccl=$nccl cuda=$cuda"
  '
