#!/bin/bash
# Allocation-scoped container device/runtime visibility gate for shared NFS images.
#SBATCH --exclusive
#SBATCH --time=00:20:00
set -euo pipefail
: "${NVIS_PREFLIGHT_IMAGE:?}"
nodes="$(scontrol show hostnames "$SLURM_JOB_NODELIST" | grep -c .)"
export MELLANOX_VISIBLE_DEVICES=all
srun --nodes="$nodes" --ntasks="$nodes" --ntasks-per-node=1 --kill-on-bad-exit=1 --label \
  --container-image="$NVIS_PREFLIGHT_IMAGE" --container-writable --no-container-mount-home \
  --container-mounts=/dev/infiniband:/dev/infiniband --container-env=MELLANOX_VISIBLE_DEVICES \
  bash -euo pipefail -c '
    test -e /dev/infiniband/rdma_cm
    test -d /sys/class/infiniband
    uverbs=$(find /dev/infiniband -maxdepth 1 -name "uverbs*" -type c 2>/dev/null | wc -l)
    (( uverbs > 0 ))
    echo "CONTAINER_IB_READY $(hostname): uverbs=$uverbs"
  '
