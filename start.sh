#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Local dashboard defaults — edit here. Override from the shell when needed,
# e.g.  PORT=8080 RACK_STATUS_DEMO=0 ./start.sh
# =============================================================================
export PORT="${PORT:-5000}"
#export RACK_STATUS_DEMO="${RACK_STATUS_DEMO:-1}"
#export RACK_WEB_AUTH_DISABLED="${RACK_WEB_AUTH_DISABLED:-1}"
export VALIDATION_TEST="${VALIDATION_TEST:-nccl_allreduce,llm}"
export RACK_WEB_PROJECT_NAME="${RACK_WEB_PROJECT_NAME:-Mistral Phase 2}"
export NEMO_SQSH_SRC="${NEMO_SQSH_SRC:-/home/cmsupport/nemo-25.11.01.sqsh}"
# Used by Flask when /api/status has no log_dir query (React SPA omits it until Settings overrides).
export RACK_STATUS_LOG_DIR="${RACK_STATUS_LOG_DIR:-/home/cmsupport/aihpc_val-main/validation/nemo/}"
# react = SPA at / ; legacy = HTML dashboard at /
export RACK_WEB_FRONTEND="${RACK_WEB_FRONTEND:-react}"
# =============================================================================

module load python3

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
NODE_VERSION="${NODE_VERSION:-22.12.0}"
LOCAL_DIR="${ROOT_DIR}/.local"

mkdir -p "$LOCAL_DIR"

echo "[start.sh] project root: $ROOT_DIR"
echo "[start.sh] frontend mode: $RACK_WEB_FRONTEND"

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
python -m pip install --upgrade pip
pip install -r requirements.txt

if [[ "$RACK_WEB_FRONTEND" == "react" || "$RACK_WEB_FRONTEND" == "spa" ]]; then
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64) NODE_ARCH="x64" ;;
    aarch64|arm64) NODE_ARCH="arm64" ;;
    *)
      echo "[start.sh] unsupported architecture for local Node download: $ARCH" >&2
      exit 1
      ;;
  esac

  NODE_BASE="node-v${NODE_VERSION}-linux-${NODE_ARCH}"
  NODE_HOME="${LOCAL_DIR}/${NODE_BASE}"
  NODE_TARBALL="${LOCAL_DIR}/${NODE_BASE}.tar.xz"

  if [[ ! -x "${NODE_HOME}/bin/node" ]]; then
    echo "[start.sh] downloading local Node.js ${NODE_VERSION} (${NODE_ARCH})"
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/${NODE_BASE}.tar.xz" -o "$NODE_TARBALL"
    tar -xJf "$NODE_TARBALL" -C "$LOCAL_DIR"
  fi

  export PATH="${NODE_HOME}/bin:${PATH}"
  echo "[start.sh] node: $(node -v), npm: $(npm -v)"

  echo "[start.sh] installing frontend deps"
  npm --prefix frontend install
  echo "[start.sh] building frontend"
  npm --prefix frontend run build
else
  echo "[start.sh] legacy mode selected; skipping frontend build"
fi

echo "[start.sh] starting Flask on port ${PORT}"
exec python app.py
