#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# NVIS Rack Status — build & run Docker container
#
# Usage:
#   ./dev-docker.sh              # dev mode (demo data, no auth, port 5050)
#   ./dev-docker.sh prod         # prod mode (SSH to head node, token auth)
#   ./dev-docker.sh build        # build only, don't run
#   ./dev-docker.sh stop         # stop and remove the container
#   ./dev-docker.sh logs         # tail container logs
#
# Override any setting with env vars:
#   RACK_WEB_PORT=8080 ./dev-docker.sh
#   RACK_SSH_HOST=10.0.0.5 RACK_PLATFORM=maas ./dev-docker.sh prod
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

IMAGE="${RACK_IMAGE:-nvis-rack-status:local}"
CONTAINER="${RACK_CONTAINER:-rack-status}"
MODE="${1:-dev}"

# ── helpers ──────────────────────────────────────────────────────────────────

usage() {
  sed -n '3,14p' "$0" | sed 's/^# \?//'
  exit 0
}

build_image() {
  echo "==> Building image ${IMAGE} …"
  docker build -t "$IMAGE" .
  echo "==> Build complete."
}

stop_container() {
  echo "==> Stopping ${CONTAINER} …"
  docker rm -f "$CONTAINER" 2>/dev/null || true
}

wait_ready() {
  local port="$1"
  echo "==> Waiting for Flask …"
  for _ in $(seq 1 15); do
    if curl -sf "http://localhost:${port}/api/status" >/dev/null 2>&1; then
      echo "==> Ready: http://localhost:${port}/spa/"
      return 0
    fi
    sleep 1
  done
  echo "==> Container may still be starting. Logs:"
  docker logs --tail 30 "$CONTAINER"
  return 1
}

run_dev() {
  local port="${RACK_WEB_PORT:-5050}"
  stop_container
  echo "==> Starting in DEV mode (demo data, no auth, port ${port}) …"
  docker run -d --name "$CONTAINER" -p "${port}:5000" \
    -e RACK_STATUS_DEMO=1 \
    -e RACK_WEB_AUTH_DISABLED=1 \
    -e RACK_EXEC_MODE=local \
    -v rack-data:/app/var \
    "$IMAGE"
  wait_ready "$port"
}

run_prod() {
  local port="${RACK_WEB_PORT:-5000}"
  local ssh_host="${RACK_SSH_HOST:-}"
  local ssh_key="${RACK_SSH_KEY_PATH:-$HOME/.ssh/id_rsa}"
  local auth_token="${RACK_WEB_AUTH_TOKEN:-}"
  local config="${RACK_CONFIG_FILE:-rack-config.env}"

  if [[ -z "$ssh_host" ]]; then
    echo "Error: RACK_SSH_HOST is required for prod mode."
    echo "  RACK_SSH_HOST=10.0.0.1 ./build-docker.sh prod"
    exit 1
  fi
  if [[ -z "$auth_token" ]]; then
    echo "Error: RACK_WEB_AUTH_TOKEN is required for prod token auth."
    echo "  RACK_WEB_AUTH_TOKEN=secret ./build-docker.sh prod"
    exit 1
  fi
  if [[ ! -f "$ssh_key" ]]; then
    echo "Warning: SSH key not found at ${ssh_key}"
  fi
  if [[ ! -f "$config" ]]; then
    echo "Warning: Config file not found at ${config}, using image defaults"
  fi

  stop_container
  echo "==> Starting in PROD mode (port ${port}, config: ${config}) …"
  docker run -d --name "$CONTAINER" -p "${port}:5000" \
    --env-file "$config" \
    -e RACK_SSH_HOST="$ssh_host" \
    -e RACK_WEB_AUTH_TOKEN="$auth_token" \
    -v "${ssh_key}:/run/secrets/ssh_key:ro" \
    --restart unless-stopped \
    "$IMAGE"
  wait_ready "$port"
}

# ── main ─────────────────────────────────────────────────────────────────────

case "$MODE" in
  dev)
    build_image
    run_dev
    ;;
  prod)
    build_image
    run_prod
    ;;
  build)
    build_image
    ;;
  stop)
    stop_container
    ;;
  logs)
    docker logs -f "$CONTAINER"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    echo "Unknown mode: $MODE"
    usage
    ;;
esac
