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
# Build a timestamped zip of this dashboard tree for copying to another cluster.
# Default output directory: /home/cmsupport  (matches .gitignore nvis-rack-status-cluster-export-*.zip)
#
# Usage:
#   ./export-dashboard-bundle.sh              -> /home/cmsupport/nvis-rack-status-cluster-export-<UTC>.zip
#   ./export-dashboard-bundle.sh /other/dir   -> /other/dir/nvis-rack-status-cluster-export-<UTC>.zip
#   ./export-dashboard-bundle.sh --dev        -> same, but include .git/ in the zip (dev handoff)
#   ./export-dashboard-bundle.sh --dev /path  -> --dev with custom output directory
#
# Excludes (default): .git, .local/ (often a vendored Node tarball under the repo — hundreds of MB), Python/Node
# caches, venvs, built SPA, var/cache, prior export zips. Optional: set INCLUDE_NVLMAPPER_BINARY=1 to
# bundle bin/scripts/nvlmapper (~14M) when that file exists.
# On the target host: unpack, pip install -r backend/requirements.txt, (cd frontend && npm ci && npm run build),
# use system Node or install Node on the target (do not rely on .local/), copy nvlmapper if needed,
# configure env — see README.md and docs/ENVIRONMENT.md.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TOP_NAME="$(basename "$ROOT")"

INCLUDE_GIT=0
OUT_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev)
      INCLUDE_GIT=1
      shift
      ;;
    -h|--help)
      sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    -*)
      echo "Unknown option: $1 (try --help)" >&2
      exit 1
      ;;
    *)
      if [[ -n "$OUT_DIR" ]]; then
        echo "Unexpected extra argument: $1 (only one output directory allowed)" >&2
        exit 1
      fi
      OUT_DIR="$1"
      shift
      ;;
  esac
done
OUT_DIR="${OUT_DIR:-/home/cmsupport}"
if [[ ! -d "$OUT_DIR" ]]; then
  echo "Output directory does not exist or is not a directory: $OUT_DIR" >&2
  exit 1
fi

TS="$(date -u +%Y%m%dT%H%M%SZ)"
ZIP_BASENAME="nvis-rack-status-cluster-export-${TS}.zip"
ZIP_PATH="$(cd "$OUT_DIR" && pwd)/${ZIP_BASENAME}"

if ! command -v zip >/dev/null 2>&1; then
  echo "zip(1) is required but not found in PATH." >&2
  exit 1
fi

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/nvis-dash-export.XXXXXX")"
cleanup() {
  rm -rf "$STAGE"
}
trap cleanup EXIT

echo "Staging from: $ROOT"
echo "Archive:      $ZIP_PATH"
if [[ "$INCLUDE_GIT" == "1" ]]; then
  echo "Mode:         --dev (including .git/)"
fi

EXCLUDE_NVL=()
if [[ "${INCLUDE_NVLMAPPER_BINARY:-}" != "1" ]]; then
  EXCLUDE_NVL=(--exclude='bin/scripts/nvlmapper')
fi

EXCLUDE_GIT=()
if [[ "$INCLUDE_GIT" != "1" ]]; then
  EXCLUDE_GIT=(--exclude='.git/')
fi

# Single top-level folder in the zip = repo basename (easy unpack on the other cluster).
# bash 3.2 + set -u: do not expand empty arrays with "${arr[@]}" — use ${arr[@]+"${arr[@]}"}.
rsync -a \
  ${EXCLUDE_NVL[@]+"${EXCLUDE_NVL[@]}"} \
  ${EXCLUDE_GIT[@]+"${EXCLUDE_GIT[@]}"} \
  --exclude='.local/' \
  --exclude='.venv/' \
  --exclude='venv/' \
  --exclude='__pycache__/' \
  --exclude='*.py[cod]' \
  --exclude='.pytest_cache/' \
  --exclude='.mypy_cache/' \
  --exclude='.ruff_cache/' \
  --exclude='*.egg-info/' \
  --exclude='.eggs/' \
  --exclude='dist/' \
  --exclude='build/' \
  --exclude='var/cache/' \
  --exclude='demo-reports/' \
  --exclude='frontend/node_modules/' \
  --exclude='frontend/dist/' \
  --exclude='static/spa/' \
  --exclude='nvis-rack-status-cluster-export-*.zip' \
  --exclude='rack-web-cluster-export-*.zip' \
  --exclude='.DS_Store' \
  --exclude='Thumbs.db' \
  --exclude='.idea/' \
  --exclude='*.swp' \
  --exclude='*.swo' \
  --exclude='*~' \
  "$ROOT/" "$STAGE/${TOP_NAME}/"

(
  cd "$STAGE"
  zip -rq "$ZIP_PATH" "$TOP_NAME"
)

echo "Done: $ZIP_PATH ($(du -h "$ZIP_PATH" | awk '{print $1}'))"
