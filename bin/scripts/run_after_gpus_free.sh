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
# run_after_gpus_free.sh — block until THIS host's GPUs are idle (no compute apps), then exec
# the given command. Queue a real acceptance run behind an in-flight burn-in without babysitting.
# Run detached (nohup) on the launch host (one of the rack's nodes).
# Author: Rafael M. Koike & Cursor Agent ("Ada")
# Usage: nohup run_after_gpus_free.sh <cmd...> > chain.log 2>&1 &   (env: MAXWAIT_MIN default 150)
set -uo pipefail
MAXWAIT_MIN="${MAXWAIT_MIN:-150}"
echo "[$(date -u +%FT%TZ)] waiting up to ${MAXWAIT_MIN}m for GPUs to go idle..."
for _ in $(seq 1 $((MAXWAIT_MIN*2))); do
  busy=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | grep -c .)
  [ "${busy:-1}" -eq 0 ] && { echo "[$(date -u +%FT%TZ)] GPUs idle"; break; }
  sleep 30
done
sleep 20
echo "[$(date -u +%FT%TZ)] launching: $*"
exec "$@"
