#!/bin/sh
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# write-build-info.sh — stamp this tree with the build identity the dashboard reports.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Writes backend/build-info.json: the release version from version.py plus a per-build stamp
# (UTC timestamp + short SHA, marked -dirty when the tree is). backend/app_version.py reads it.
#
# Why the tree and not the image
#   Deployments bind-mount the backend directory over the image's, so a copy baked in under that
#   path is hidden at runtime. /app/var is worse: it is masked by a named volume, which Docker seeds
#   from the image only while the volume is empty — so on any deployment that has ever run, a file
#   written there is invisible. Writing it here and letting the Dockerfile's `COPY backend/` carry
#   it in means the mounted deployment and a bare `docker run` read the same file.
#
# Run it before building:
#   bin/write-build-info.sh && docker compose build && docker compose up -d
#
# Absent, app_version.py falls back to parsing version.py and reports "<version>-dev", which is the
# honest answer for a tree nobody built.
#
# Usage:
#   bin/write-build-info.sh [--stamp STAMP]      # default stamp: <UTC>.<short-sha>[-dirty]
#
# POSIX sh, not bash, on purpose: the Kaniko :debug image CI builds with is busybox and has no bash,
# where a bash shebang fails as "not found" (exit 127) and takes the image build with it.
set -u

HERE="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$HERE/backend/build-info.json"

STAMP=""
[ "${1:-}" = "--stamp" ] && { STAMP="${2:-}"; }

if [ -z "$STAMP" ]; then
  ts="$(date -u +%Y%m%dT%H%M)"
  sha="$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo nogit)"
  # A dirty tree is a different artifact from the commit it is based on; say so rather than imply
  # the build matches that commit.
  git -C "$HERE" diff --quiet HEAD 2>/dev/null || sha="${sha}-dirty"
  STAMP="${ts}.${sha}"
fi

# Parsed, never imported: version.py is release metadata read by CI's grep, and importing it would
# run module code here for no reason.
VERSION="$(sed -n 's/^ *VERSION *= *["'"'"']\([^"'"'"']*\)["'"'"'].*/\1/p' "$HERE/version.py" 2>/dev/null | head -1)"

printf '{"version": "%s", "build": "%s"}\n' "${VERSION:-unknown}" "$STAMP" > "$OUT" \
  || { echo "write-build-info: could not write $OUT" >&2; exit 1; }

printf 'wrote %s\n  %s\n' "${OUT#"$HERE"/}" "$(cat "$OUT")"
