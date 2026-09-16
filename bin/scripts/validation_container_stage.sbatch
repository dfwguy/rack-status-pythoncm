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
: "${NVIS_STAGE_GPU_RECOVERY_CHECK:=0}"
: "${NVIS_STAGE_ENSURE_IMEX_CHANNEL0:=0}"
: "${NVIS_STAGE_EXPECTED_SIZE:?NVIS_STAGE_EXPECTED_SIZE is required}"
: "${NVIS_STAGE_EXPECTED_SHA256:?NVIS_STAGE_EXPECTED_SHA256 is required}"
: "${NVIS_SCRIPTS_DIR:=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
: "${NVIS_SLURM_BIN:=/cm/local/apps/slurm/25.05/bin}"
[[ -x "$NVIS_SLURM_BIN/scontrol" && -x "$NVIS_SLURM_BIN/srun" ]] || {
  echo "ERROR: pinned Slurm tools unavailable under $NVIS_SLURM_BIN" >&2
  exit 2
}

nodes="$("$NVIS_SLURM_BIN/scontrol" show hostnames "${SLURM_JOB_NODELIST}" | grep -c .)"
(( nodes > 0 )) || { echo "ERROR: allocation contains no nodes" >&2; exit 2; }

case "$NVIS_STAGE_ENSURE_IMEX_CHANNEL0" in
  0|1) ;;
  *) echo "ERROR: NVIS_STAGE_ENSURE_IMEX_CHANNEL0 must be 0 or 1" >&2; exit 2 ;;
esac

# GB300 CUDA fabric handles require channel0 to exist on the host.  NVIDIA's
# driver registers the character-device major but does not create channel0 by
# default on this fleet.  Create it once, inside the exact-node allocation and
# for the Slurm submit identity, before any NCCL/NeMo/MNUberGEMM workload is
# submitted.  This is deliberately ephemeral: no systemd unit, timer, udev rule,
# module option, or background reconciler is installed.
if [[ "$NVIS_STAGE_ENSURE_IMEX_CHANNEL0" == 1 ]]; then
  "$NVIS_SLURM_BIN/srun" --kill-on-bad-exit=1 --nodes="${nodes}" --ntasks="${nodes}" --ntasks-per-node=1 \
    bash -euo pipefail -c '
      path=/dev/nvidia-caps-imex-channels/channel0
      major="$(awk '\''$2 == "nvidia-caps-imex-channels" { print $1 }'\'' /proc/devices)"
      case "$major" in
        ""|*[!0-9]*)
          echo "IMEX_CHANNEL_FAIL $(hostname): invalid registered major=${major:-missing}" >&2
          exit 30
          ;;
      esac
      submit_gid="$(id -g)"
      sudo -n install -d -o root -g root -m 0755 "$(dirname "$path")"
      if [[ -e "$path" ]]; then
        [[ -c "$path" ]] || {
          echo "IMEX_CHANNEL_FAIL $(hostname): path exists but is not a character device: $path" >&2
          exit 31
        }
      else
        sudo -n mknod "$path" c "$major" 0
      fi
      expected_dev="$(printf "%x:0" "$major")"
      actual_dev="$(stat -Lc "%t:%T" "$path")"
      [[ "$actual_dev" == "$expected_dev" ]] || {
        echo "IMEX_CHANNEL_FAIL $(hostname): device=$actual_dev expected=$expected_dev path=$path" >&2
        exit 32
      }
      sudo -n chown "0:${submit_gid}" "$path"
      sudo -n chmod 0660 "$path"
      actual_access="$(stat -Lc "%a:%u:%g" "$path")"
      [[ "$actual_access" == "660:0:${submit_gid}" ]] || {
        echo "IMEX_CHANNEL_FAIL $(hostname): access=$actual_access expected=660:0:${submit_gid}" >&2
        exit 33
      }
      python3 -c '\''import os; path="/dev/nvidia-caps-imex-channels/channel0"; fd=os.open(path, os.O_RDWR); os.close(fd)'\''
      echo "IMEX_CHANNEL_READY $(hostname): major=$major minor=0 mode=660 uid=0 gid=$submit_gid path=$path"
    '
fi

export NVIS_STAGE_SOURCE NVIS_STAGE_DEST NVIS_STAGE_RSYNC
export NVIS_STAGE_EXPECTED_SIZE NVIS_STAGE_EXPECTED_SHA256
export NVIS_STAGE_EXPECTED_GPUS NVIS_STAGE_EXPECTED_NCCL NVIS_STAGE_EXPECTED_CUDA
"$NVIS_SLURM_BIN/srun" --kill-on-bad-exit=1 --nodes="${nodes}" --ntasks="${nodes}" --ntasks-per-node=1 \
  bash -euo pipefail -c '
    source_path="$NVIS_STAGE_SOURCE"
    destination="$NVIS_STAGE_DEST"
    source_size="$(stat -c %s "$source_path" 2>/dev/null || true)"
    [[ "$source_size" == "$NVIS_STAGE_EXPECTED_SIZE" ]] || {
      echo "IMAGE_STAGE_FAIL $(hostname): source size expected=$NVIS_STAGE_EXPECTED_SIZE actual=${source_size:-missing}: $source_path" >&2
      exit 2
    }
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
    # /opt/containers is deliberately root-owned 0755.  A pre-staged image only
    # needs the read-only gates above; take a user-owned lock in /tmp only when
    # this allocation really must copy, then use the site-approved passwordless
    # sudo path for writes into the root-owned cache.
    lock="/tmp/.nvis-stage-${UID}-${NVIS_STAGE_EXPECTED_SHA256}.lock"
    exec 9>"$lock"
    flock -x 9
    destination_size="$(stat -c %s "$destination" 2>/dev/null || true)"
    if [[ "$destination_size" == "$NVIS_STAGE_EXPECTED_SIZE" && -r "$destination" ]]; then
      destination_sha="$(sha256sum "$destination" | awk "{print \$1}")"
      if [[ "$destination_sha" == "$NVIS_STAGE_EXPECTED_SHA256" ]]; then
        echo "IMAGE_STAGE_READY $(hostname): bytes=$destination_size sha256=$destination_sha cached=yes path=$destination"
        exit 0
      fi
    fi
    sudo -n install -d -m 0755 "$(dirname "$destination")"
    partial="${destination}.partial"
    sudo -n rsync -a --partial --append-verify -- "$source_path" "$partial"
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
    sudo -n mv -- "$partial" "$destination"
    echo "IMAGE_STAGE_READY $(hostname): bytes=$partial_size sha256=$partial_sha cached=no path=$destination"
  '

# NeMo allocations must reject trays that Slurm still reports IDLE while the GPU
# driver requires a reset.  Keep this on the allocation side: the Rack Status web
# container intentionally has neither pdsh nor compute-node SSH credentials.
if [[ "${NVIS_STAGE_GPU_RECOVERY_CHECK}" == 1 ]]; then
  export NVIS_STAGE_EXPECTED_GPUS
  "$NVIS_SLURM_BIN/srun" --kill-on-bad-exit=1 --nodes="${nodes}" --ntasks="${nodes}" --ntasks-per-node=1 \
    bash -euo pipefail -c '
      expected="$NVIS_STAGE_EXPECTED_GPUS"
      (( expected > 0 )) || expected=4
      if ! actions="$(nvidia-smi --query-gpu=gpu_recovery_action --format=csv,noheader 2>&1)"; then
        echo "GPU_RECOVERY_FAIL $(hostname): query=$(printf "%s" "$actions" | tr "\n" ",")" >&2
        exit 20
      fi
      total="$(printf "%s\n" "$actions" | awk "NF { n++ } END { print n + 0 }")"
      ready="$(printf "%s\n" "$actions" | awk "\$0 == \"None\" { n++ } END { print n + 0 }")"
      if [[ "$total" != "$expected" || "$ready" != "$expected" ]]; then
        echo "GPU_RECOVERY_FAIL $(hostname): ready=$ready total=$total expected=$expected actions=$(printf "%s" "$actions" | tr "\n" ",")" >&2
        exit 21
      fi
      echo "GPU_RECOVERY_READY $(hostname): ready=$ready expected=$expected"
    '
fi

if [[ "${NVIS_STAGE_CONTAINER_PREFLIGHT}" == 1 ]]; then
  export MELLANOX_VISIBLE_DEVICES=all
  "$NVIS_SLURM_BIN/srun" --kill-on-bad-exit=1 --nodes="${nodes}" --ntasks="${nodes}" --ntasks-per-node=1 \
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
