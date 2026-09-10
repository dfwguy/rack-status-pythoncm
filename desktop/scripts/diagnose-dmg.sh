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
#
# diagnose-dmg.sh -- find which process holds the DMG volume open during a build.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# `npm run dist` fails with `hdiutil: couldn't unmount "diskN" - Resource busy`. That message names
# the device but never the culprit, and the mount only exists for a few seconds mid-build, so it
# cannot be inspected afterwards. This runs the build and samples `lsof` against the volume while
# it is up.
#
# Deliberately uses `df` and `lsof` rather than `hdiutil`/`diskutil`: the question is "which
# process has a file open here", which lsof answers, and this then works without elevated tooling.

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
OUT=${1:-/tmp/dmg-diagnosis.txt}
: > "$OUT"

echo "starting dmg build in the background..."
rm -rf release
npx electron-builder --mac dmg --arm64 --publish never > /tmp/dmg-build.log 2>&1 &
BUILD_PID=$!

echo "watching for the volume (up to 120s)..."
FOUND=""
for _ in $(seq 1 600); do
  # `df` rather than `mount`: same information for this purpose, and it reports the mount point
  # directly. Grep is case-insensitive because the volume name is the productName.
  MOUNT=$(df 2>/dev/null | grep -i greenlight | sed 's|^.*% *||' | head -1)
  if [ -n "$MOUNT" ] && [ -d "$MOUNT" ]; then
    FOUND="$MOUNT"
    {
      echo "=== volume appeared: $MOUNT ==="
      date '+%H:%M:%S.%N'
      echo
      echo "--- df ---"
      df -h "$MOUNT"
      echo
      # Three samples: a holder that appears only in one of them is a transient scanner finishing
      # its work, which is a different problem from one that holds the volume for its whole life.
      for i in 1 2 3; do
        echo "--- lsof sample $i ($(date '+%H:%M:%S')) ---"
        # `-w` suppresses warnings about unreadable dirs; without it the real output is buried.
        lsof -nP -w -- "$MOUNT" 2>/dev/null || echo "(lsof returned nothing)"
        echo
        sleep 1
      done
    } >> "$OUT" 2>&1
    break
  fi
  sleep 0.2
done

if [ -z "$FOUND" ]; then
  echo "the volume never appeared in df -- the build may have failed earlier" | tee -a "$OUT"
fi

wait $BUILD_PID
BUILD_RC=$?
{
  echo "=== build exit: $BUILD_RC ==="
  echo "--- build error tail ---"
  grep -iE 'resource busy|couldn.t unmount|DMGError|⨯' /tmp/dmg-build.log | head -10
} >> "$OUT" 2>&1

echo
echo "diagnosis written to $OUT"
cat "$OUT"
