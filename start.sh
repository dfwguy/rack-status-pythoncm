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
set -euo pipefail

# =============================================================================
# NVIS Rack Status — start script
#
# Modes:
#   ./start.sh                              # auto-detect: demo on macOS, cluster on Linux
#   RACK_STATUS_DEMO=1 ./start.sh           # force demo mode
#   RACK_STATUS_DEMO=0 ./start.sh           # force cluster mode
# =============================================================================

export PORT="${PORT:-5050}"

# ---- Auto-detect mode based on OS ----
# macOS = local development (demo mode by default)
# Linux = HPC cluster (real mode by default)
if [[ "$(uname -s)" == "Darwin" ]]; then
  export RACK_STATUS_DEMO="${RACK_STATUS_DEMO:-1}"
  export RACK_WEB_AUTH_DISABLED="${RACK_WEB_AUTH_DISABLED:-1}"
else
  export RACK_STATUS_DEMO="${RACK_STATUS_DEMO:-0}"
fi

# ---- Cluster defaults (only applied when NOT in demo mode) ----
if [[ "${RACK_STATUS_DEMO}" != "1" ]]; then
  export VALIDATION_TEST="${VALIDATION_TEST:-nccl_allreduce,llm}"
  export NEMO_SQSH_SRC="${NEMO_SQSH_SRC:-/home/cmsupport/nemo-25.11.01.sqsh}"
  export RACK_STATUS_LOG_DIR="${RACK_STATUS_LOG_DIR:-/home/cmsupport/aihpc_val-main/validation/nemo}" 
fi
# =============================================================================

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# ---- OS and architecture detection ----
OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
  Darwin) NODE_OS="darwin" ;;
  Linux)  NODE_OS="linux" ;;
  *)
    echo "[start.sh] unsupported OS: $OS" >&2
    exit 1
    ;;
esac

case "$ARCH" in
  x86_64)       NODE_ARCH="x64" ;;
  aarch64|arm64) NODE_ARCH="arm64" ;;
  *)
    echo "[start.sh] unsupported architecture: $ARCH" >&2
    exit 1
    ;;
esac

echo "[start.sh] detected OS=$OS ($NODE_OS), arch=$ARCH ($NODE_ARCH)"

# ---- Environment modules (BCM head nodes only) ----
if [[ "$OS" == "Linux" ]] && command -v module &>/dev/null; then
  module load python3 2>/dev/null || true
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
NODE_VERSION="${NODE_VERSION:-22.12.0}"
LOCAL_DIR="${ROOT_DIR}/.local"

mkdir -p "$LOCAL_DIR"

echo "[start.sh] project root: $ROOT_DIR"
echo "[start.sh] frontend: React SPA"

# ---- Python virtual environment ----
if [[ ! -f ".venv/bin/activate" ]]; then
  if [[ -d ".venv" ]]; then
    echo "[start.sh] .venv exists but has no bin/activate (incomplete); removing and recreating"
    rm -rf .venv
  fi
  echo "[start.sh] creating python venv with ${PYTHON_BIN}"
  "$PYTHON_BIN" -m venv .venv
fi

if [[ ! -f ".venv/bin/activate" ]]; then
  echo "[start.sh] failed: .venv/bin/activate still missing after venv create. Check ${PYTHON_BIN} -m venv." >&2
  exit 1
fi

# shellcheck disable=SC1091
source ".venv/bin/activate"

echo "[start.sh] installing python deps"
python -m pip install --upgrade pip -q
pip install -r backend/requirements.txt -q

# ---- Frontend build (React SPA) ----
# ---- Frontend build (React SPA) ----
BUILD_FRONTEND="${BUILD_FRONTEND:-1}"
if [[ "$BUILD_FRONTEND" == "1" ]]; then

  # Check if system Node.js is already available and sufficient
  if command -v node &>/dev/null; then
    SYSTEM_NODE_VERSION="$(node -v | sed 's/v//')"
    SYSTEM_NODE_MAJOR="${SYSTEM_NODE_VERSION%%.*}"
    if [[ "$SYSTEM_NODE_MAJOR" -ge 20 ]]; then
      echo "[start.sh] using system Node.js $(node -v)"
      USE_SYSTEM_NODE=1
    fi
  fi

  if [[ "${USE_SYSTEM_NODE:-}" != "1" ]]; then
    # Download platform-specific Node.js binary
    NODE_BASE="node-v${NODE_VERSION}-${NODE_OS}-${NODE_ARCH}"
    NODE_HOME="${LOCAL_DIR}/${NODE_BASE}"
    NODE_TARBALL="${LOCAL_DIR}/${NODE_BASE}.tar.xz"

    if [[ ! -x "${NODE_HOME}/bin/node" ]]; then
      echo "[start.sh] downloading local Node.js ${NODE_VERSION} (${NODE_OS}-${NODE_ARCH})"
      curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/${NODE_BASE}.tar.xz" -o "$NODE_TARBALL"
      tar -xJf "$NODE_TARBALL" -C "$LOCAL_DIR"
    fi

    export PATH="${NODE_HOME}/bin:${PATH}"
  fi

  echo "[start.sh] node: $(node -v), npm: $(npm -v)"

  echo "[start.sh] installing frontend deps"
  npm --prefix frontend install
  echo "[start.sh] building frontend"
  npm --prefix frontend run build
else
  echo "[start.sh] skipping frontend build (BUILD_FRONTEND=0)"
fi

echo "[start.sh] starting Flask on port ${PORT}"
# Run from the repo root (cwd) so backend modules resolve repo-root assets
# (bin/, static/spa, var/, help_content.json); app.py adds backend/ to sys.path itself.
exec python backend/app.py
