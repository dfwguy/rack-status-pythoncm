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
# One-shot, exact-allocation IMEX channel0 preparation for host-runtime launchers.
# This script intentionally installs no service, timer, udev rule, or module option.
#SBATCH --job-name=nvis-imex-channel0
#SBATCH --exclusive
#SBATCH --time=00:05:00

set -euo pipefail

: "${NVIS_SLURM_BIN:=/cm/local/apps/slurm/25.05/bin}"
[[ -x "$NVIS_SLURM_BIN/scontrol" && -x "$NVIS_SLURM_BIN/srun" ]] || {
  echo "ERROR: pinned Slurm tools unavailable under $NVIS_SLURM_BIN" >&2
  exit 2
}

nodes="$("$NVIS_SLURM_BIN/scontrol" show hostnames "${SLURM_JOB_NODELIST}" | grep -c .)"
(( nodes > 0 )) || { echo "ERROR: allocation contains no nodes" >&2; exit 2; }

"$NVIS_SLURM_BIN/srun" --kill-on-bad-exit=1 --nodes="$nodes" --ntasks="$nodes" --ntasks-per-node=1 \
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

echo "IMEX_CHANNEL_COMPLETE nodes=$nodes"
