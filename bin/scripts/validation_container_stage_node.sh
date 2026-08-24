#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Per-node half of the validation image stage gate, launched by PythonCM.

set -euo pipefail

: "${NVIS_STAGE_SOURCE:?NVIS_STAGE_SOURCE is required}"
: "${NVIS_STAGE_DEST:?NVIS_STAGE_DEST is required}"
: "${NVIS_STAGE_RSYNC:=0}"
: "${NVIS_STAGE_EXPECTED_SIZE:?NVIS_STAGE_EXPECTED_SIZE is required}"
: "${NVIS_STAGE_EXPECTED_SHA256:?NVIS_STAGE_EXPECTED_SHA256 is required}"

source_size="$(stat -c %s "$NVIS_STAGE_SOURCE" 2>/dev/null || true)"
[[ "$source_size" == "$NVIS_STAGE_EXPECTED_SIZE" ]] || {
  echo "IMAGE_STAGE_FAIL $(hostname): source size expected=$NVIS_STAGE_EXPECTED_SIZE actual=${source_size:-missing}: $NVIS_STAGE_SOURCE" >&2
  exit 2
}

mkdir -p "$(dirname "$NVIS_STAGE_DEST")"
lock="${NVIS_STAGE_DEST}.nvis-stage.lock"
exec 9>"$lock"
flock -x 9
destination_size="$(stat -c %s "$NVIS_STAGE_DEST" 2>/dev/null || true)"
if [[ "$destination_size" == "$NVIS_STAGE_EXPECTED_SIZE" && -r "$NVIS_STAGE_DEST" ]]; then
  destination_sha="$(sha256sum "$NVIS_STAGE_DEST" | awk '{print $1}')"
  if [[ "$destination_sha" == "$NVIS_STAGE_EXPECTED_SHA256" ]]; then
    echo "IMAGE_STAGE_READY $(hostname): bytes=$destination_size sha256=$destination_sha cached=yes path=$NVIS_STAGE_DEST"
    exit 0
  fi
fi

if [[ "$NVIS_STAGE_RSYNC" != 1 || "$NVIS_STAGE_SOURCE" == "$NVIS_STAGE_DEST" ]]; then
  echo "IMAGE_STAGE_FAIL $(hostname): expected=$NVIS_STAGE_EXPECTED_SIZE actual=${destination_size:-missing} path=$NVIS_STAGE_DEST rsync=$NVIS_STAGE_RSYNC" >&2
  exit 3
fi

partial="${NVIS_STAGE_DEST}.partial"
rsync -a --partial --append-verify -- "$NVIS_STAGE_SOURCE" "$partial"
partial_size="$(stat -c %s "$partial" 2>/dev/null || true)"
[[ "$partial_size" == "$NVIS_STAGE_EXPECTED_SIZE" ]] || {
  echo "IMAGE_STAGE_FAIL $(hostname): partial expected=$NVIS_STAGE_EXPECTED_SIZE actual=${partial_size:-missing}" >&2
  exit 4
}
partial_sha="$(sha256sum "$partial" | awk '{print $1}')"
[[ "$partial_sha" == "$NVIS_STAGE_EXPECTED_SHA256" ]] || {
  echo "IMAGE_STAGE_FAIL $(hostname): partial sha256=$partial_sha expected=$NVIS_STAGE_EXPECTED_SHA256" >&2
  exit 5
}
mv -- "$partial" "$NVIS_STAGE_DEST"
echo "IMAGE_STAGE_READY $(hostname): bytes=$partial_size sha256=$partial_sha cached=no path=$NVIS_STAGE_DEST"
