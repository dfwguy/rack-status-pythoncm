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

# ---- Runtime settings now belong to the app, not to this script ----
#
# Demo-vs-cluster mode and the three cluster-only defaults moved into
# `backend/cli.py::apply_runtime_defaults` on 2026-09-08. A decision made here was invisible to the
# app that depends on it, untestable by the suite that verifies the app, and re-derived by every
# other launcher; the app now infers mode from the platform itself, with the same `setdefault`
# semantics, so `RACK_STATUS_DEMO=1 ./start.sh` still works exactly as before.
#
# `RACK_WEB_AUTH_DISABLED=1` on macOS is GONE and must not come back. A developer running with
# authentication off is not running the product, and it was the reason the suite and the local run
# both skipped the gate for months. First boot creates an administrator, prints a one-time
# password, and writes the policy that grants them — so a local run is a real, authenticated,
# policy-enforcing deployment. `bin/run-local-auth.sh` is the same thing with an isolated store.
#
# This script keeps what genuinely precedes Python: the virtualenv, the Node toolchain, the frontend
# build, and `module load` on a BCM head node.
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
# Node.js to fetch when the system has none we accept. Keep this on the current **Active LTS**
# line and pin it exactly -- the URL below is a literal tarball, so a version that does not exist
# is a 404 and a dead bootstrap rather than a warning. `.nvmrc` names the same major.
NODE_VERSION="${NODE_VERSION:-24.20.0}"
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
    # 22, not 24, and not the 20 this used to accept. The floor is "oldest line still receiving
    # security fixes", which is a different question from "which line do we build on": Node 20
    # went end-of-life in April 2026, so accepting it was shipping an unpatched toolchain, while
    # requiring 24 would make every developer already on 22 download a second Node for no gain
    # (Vite needs >=22.12). CI, Docker and the download above all use the Active LTS.
    if [[ "$SYSTEM_NODE_MAJOR" -ge 22 ]]; then
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

# Run from the repo root (cwd) so backend modules resolve repo-root assets
# (bin/, static/spa, var/, help_content.json); app.py adds backend/ to sys.path itself.
#
# Handed to `backend/cli.py serve`, which owns the gunicorn invocation, the TLS wiring and the
# startup gate (an administrator, a policy and a certificate — created on a first run, refused if
# this is an existing deployment that has never been given a policy). One launch path, so a fix to
# the server's flags cannot land in one launcher and miss the other.
#
# TLS runs under gunicorn, NOT the Werkzeug development server.
#
# The dev server closes the connection after every response (no keep-alive) while streaming
# chunked, brotli-compressed bodies. Over TLS that is a shutdown race: when close_notify does not
# land cleanly before the TCP FIN, Chrome sees a truncated chunked stream and retries until it
# gives up with ERR_TOO_MANY_RETRIES. It hits the largest asset first -- the 331 KB CSS bundle --
# and then "fixes itself" on reload because the asset comes from cache, which is what makes it
# look like a UI bug rather than a server one. curl does not reproduce it; it tolerates the EOF
# pattern Chrome rejects.
#
# Gunicorn is what the container runs, supports keep-alive, and does not do this. Same
# gunicorn.conf.py, so the local run gets the same TLS floor as production.
echo "[start.sh] handing off to backend/cli.py serve on port ${PORT}"
exec python backend/cli.py serve --port "${PORT}"
