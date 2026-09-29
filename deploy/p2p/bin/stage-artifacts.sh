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
#
# Stage the pinned Dragonfly binaries into deploy/p2p/dist/ and verify them against
# deploy/p2p/artifacts.lock. This is the one step that turns the lean, text-only repo into a
# self-contained image: the binaries are ~300 MB and are never committed, so a build stages them
# from a validated source set and refuses to proceed unless every byte matches the lock.
#
#   P2P_ARTIFACT_SOURCE_DIR=/path/to/validated/dist  deploy/p2p/bin/stage-artifacts.sh
#
# The source directory holds the same layout as the lock (core/, client-amd64-v1.4.9/,
# client-arm64-64k-v1.4.9/). It is operator-provided precisely because the arm64-64k client is a
# custom build with no upstream release to fetch (see artifacts.lock) -- reproducibility comes from
# the sha256 pin, not from trusting a download URL.
#
# Idempotent: a dist/ that already satisfies the lock is left untouched and the build is not slowed.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # deploy/p2p
lock="$here/artifacts.lock"
dist="$here/dist"

[[ -r "$lock" ]] || { echo "stage-artifacts: missing $lock" >&2; exit 66; }

verify() {
  # `sha256sum -c` against the lock, from within dist/ so the paths resolve. Returns non-zero on any
  # missing or mismatched file. Comments/blank lines are stripped first: sha256sum -c has no comment
  # syntax and would warn on (and, under some coreutils, fail) every commented line.
  grep -vE '^[[:space:]]*(#|$)' "$lock" | ( cd "$dist" && sha256sum --quiet -c - ) >/dev/null 2>&1
}

if [[ -d "$dist" ]] && verify; then
  echo "stage-artifacts: dist/ already matches artifacts.lock — nothing to do."
  exit 0
fi

src="${P2P_ARTIFACT_SOURCE_DIR:-}"
if [[ -z "$src" ]]; then
  cat >&2 <<EOF
stage-artifacts: deploy/p2p/dist/ is missing or does not match artifacts.lock, and
                 P2P_ARTIFACT_SOURCE_DIR is not set.

Point it at a validated binary set with the layout in artifacts.lock, e.g.:

  P2P_ARTIFACT_SOURCE_DIR=/path/to/nvis-p2p/dist $0

The arm64-64k client is a custom build and is never fetched from the internet; it must come from
the validated artifact set. Every file is checksum-verified against artifacts.lock before use.
EOF
  exit 67
fi

echo "stage-artifacts: staging from $src"
mkdir -p "$dist"
# Copy exactly the files the lock names — nothing else — so an unexpected file in the source cannot
# ride into the image.
while read -r sha rel; do
  [[ -z "$sha" || "$sha" == \#* ]] && continue
  srcf="$src/$rel"
  [[ -r "$srcf" ]] || { echo "stage-artifacts: source missing $rel ($srcf)" >&2; exit 68; }
  mkdir -p "$dist/$(dirname "$rel")"
  cp -f "$srcf" "$dist/$rel"
  chmod 0755 "$dist/$rel"
done < <(grep -vE '^[[:space:]]*(#|$)' "$lock")

if ! verify; then
  echo "stage-artifacts: staged tree FAILED checksum verification against artifacts.lock" >&2
  exit 69
fi
echo "stage-artifacts: staged and verified $(grep -cvE '^[[:space:]]*(#|$)' "$lock") artifact(s) into dist/"
