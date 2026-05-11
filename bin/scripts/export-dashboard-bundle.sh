#!/usr/bin/env bash
# Build a timestamped zip of this dashboard tree for copying to another cluster.
# Default output directory: /home/cmsupport  (matches .gitignore nvis-rack-status-cluster-export-*.zip)
#
# Usage:
#   ./export-dashboard-bundle.sh              -> /home/cmsupport/nvis-rack-status-cluster-export-<UTC>.zip
#   ./export-dashboard-bundle.sh /other/dir   -> /other/dir/nvis-rack-status-cluster-export-<UTC>.zip
#
# Excludes: .git, .local/ (often a vendored Node tarball under the repo — hundreds of MB), Python/Node
# caches, venvs, built SPA, var/cache, prior export zips. Optional: set INCLUDE_NVLMAPPER_BINARY=1 to
# bundle bin/scripts/nvlmapper (~14M) when that file exists.
# On the target host: unpack, pip install -r requirements.txt, (cd frontend && npm ci && npm run build),
# use system Node or install Node on the target (do not rely on .local/), copy nvlmapper if needed,
# configure env — see README.md and docs/ENVIRONMENT.md.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TOP_NAME="$(basename "$ROOT")"

OUT_DIR="${1:-/home/cmsupport}"
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

EXCLUDE_NVL=()
if [[ "${INCLUDE_NVLMAPPER_BINARY:-}" != "1" ]]; then
  EXCLUDE_NVL=(--exclude='bin/scripts/nvlmapper')
fi

# Single top-level folder in the zip = repo basename (easy unpack on the other cluster).
rsync -a \
  "${EXCLUDE_NVL[@]}" \
  --exclude='.git/' \
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
