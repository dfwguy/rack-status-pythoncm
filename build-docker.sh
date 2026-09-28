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
# ──────────────────────────────────────────────────────────────────────────────
# NVIS Rack Status — build, run, and package the Docker container
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

IMAGE="${RACK_IMAGE:-nvis-rack-status:local}"
CONTAINER="${RACK_CONTAINER:-rack-status}"
MODE="${1:-dev}"

# ── helpers ──────────────────────────────────────────────────────────────────

usage() {
  cat <<'EOF'
NVIS Rack Status — build, run, and package the Docker container

Usage:
  ./build-docker.sh                # dev mode (demo data, HTTPS, login required, port 5050)
  ./build-docker.sh prod           # prod mode (SSH to head node; settings in config/rackstatus.toml)
  ./build-docker.sh build          # build the image only, don't run
  ./build-docker.sh save           # build + `docker save` → dist/*.tar.gz (air-gapped artifact)
  ./build-docker.sh load <tar>     # `docker load` an image tarball on an air-gapped host
  ./build-docker.sh stop           # stop and remove the container
  ./build-docker.sh logs           # tail container logs

Settings live in config/rackstatus.toml (mounted read-only). On a first prod run with no file,
RACK_SSH_HOST creates the minimal one; after that, edit the file:
  RACK_SSH_HOST=10.0.0.5 ./build-docker.sh prod
The host port and bind address are the only run-time overrides:
  RACK_WEB_PORT=8080 RACK_WEB_BIND_ADDR=127.0.0.1 ./build-docker.sh

Image-bundle (save) env vars:
  RACK_IMAGE_OUT_DIR=dist          # output directory for the .tar(.gz) + .sha256
  RACK_IMAGE_TAG=0.1.2             # image tag (defaults to version.py VERSION)
  RACK_IMAGE_COMPRESS=1            # 1 = gzip the tar (default), 0 = leave uncompressed
  RACK_SAVE_IMAGE=nvis-rack-status:0.1.2   # full image ref to build/save (overrides tag)
EOF
  exit 0
}

# Read VERSION from version.py at the repo root (the canonical release-metadata
# file consumed by the ci-toolkit release jobs). e.g. VERSION = "0.1.2".
get_version() {
  local v
  v="$(grep -Eo '"[0-9]+\.[0-9]+\.[0-9]+[^"]*"' version.py 2>/dev/null | head -1 | tr -d '"')"
  echo "${v:-0.0.0}"
}

# Portable sha256 helpers (sha256sum on Linux, shasum on macOS).
sha256_write() {
  local f="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    ( cd "$(dirname "$f")" && sha256sum "$(basename "$f")" > "$(basename "$f").sha256" )
  else
    ( cd "$(dirname "$f")" && shasum -a 256 "$(basename "$f")" > "$(basename "$f").sha256" )
  fi
}

sha256_check() {
  local sumfile="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    ( cd "$(dirname "$sumfile")" && sha256sum -c "$(basename "$sumfile")" )
  else
    ( cd "$(dirname "$sumfile")" && shasum -a 256 -c "$(basename "$sumfile")" )
  fi
}

# Per-build identity, baked into var/build-info.json so the dashboard can report which image it
# is running. version.py alone cannot distinguish two builds of the same release commit, and this
# is the string an operator reads back to us when something is wrong on a customer cluster.
get_build_stamp() {
  local ts sha
  ts="$(date -u +%Y%m%dT%H%M)"
  sha="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
  # A dirty tree is a different artifact from the commit it is based on; say so.
  if ! git diff --quiet HEAD 2>/dev/null; then sha="${sha}-dirty"; fi
  echo "${ts}.${sha}"
}

build_image() {
  local stamp
  stamp="$(get_build_stamp)"
  # Stamp the TREE, not the image: the Dockerfile's COPY carries backend/build-info.json in, and a
  # deployment that bind-mounts backend/ over the image would hide a copy generated at build time.
  "$(dirname "${BASH_SOURCE[0]}")/bin/write-build-info.sh" --stamp "$stamp" >/dev/null
  echo "==> Building image ${IMAGE} (build ${stamp}) …"
  docker build -t "$IMAGE" .
  echo "==> Build complete."
}

# Build the image with a versioned tag and `docker save` it to a portable tarball
# that can be transferred to an air-gapped host and `docker load`ed there.
save_image() {
  local version tag image_ref latest_ref out_dir tar_path compress
  version="$(get_version)"
  tag="${RACK_IMAGE_TAG:-$version}"
  image_ref="${RACK_SAVE_IMAGE:-nvis-rack-status:${tag}}"
  latest_ref="nvis-rack-status:latest"
  out_dir="${RACK_IMAGE_OUT_DIR:-dist}"
  compress="${RACK_IMAGE_COMPRESS:-1}"

  local stamp
  stamp="$(get_build_stamp)"
  "$(dirname "${BASH_SOURCE[0]}")/bin/write-build-info.sh" --stamp "$stamp" >/dev/null
  echo "==> Building image ${image_ref} (+ ${latest_ref}, build ${stamp}) …"
  docker build -t "$image_ref" -t "$latest_ref" .

  mkdir -p "$out_dir"
  tar_path="${out_dir}/nvis-rack-status-${tag}.tar"

  echo "==> Saving image → ${tar_path} (docker save) …"
  docker save -o "$tar_path" "$image_ref" "$latest_ref"

  if [[ "$compress" != "0" ]]; then
    echo "==> Compressing → ${tar_path}.gz …"
    gzip -f "$tar_path"
    tar_path="${tar_path}.gz"
  fi

  sha256_write "$tar_path"

  echo
  echo "==> Image artifact ready:"
  ls -lh "$tar_path" "${tar_path}.sha256"
  cat <<EOF

Transfer the files below to the air-gapped host, then load and run them:

  sha256sum -c $(basename "$tar_path").sha256      # verify integrity
  docker load -i $(basename "$tar_path")           # import the image (gz auto-detected)
  docker run -d --name rack-status -p 5000:5000 \\
    -e RACK_STATUS_DEMO=1 \\
    -v rack-data:/app/var ${image_ref}

Then open https://<host>:5000/spa/ and sign in as 'admin'. The first start bootstraps an
administrator, a policy and a self-signed certificate — read the one-time password with:

  docker logs rack-status 2>&1 | grep -A6 "ONE-TIME ADMINISTRATOR"
EOF
}

# Load a previously saved image tarball (for air-gapped hosts).
load_image() {
  local tar_path="${1:-${RACK_IMAGE_TAR:-}}"
  if [[ -z "$tar_path" ]]; then
    echo "Error: provide the image tarball path."
    echo "  ./build-docker.sh load dist/nvis-rack-status-<version>.tar.gz"
    exit 1
  fi
  if [[ ! -f "$tar_path" ]]; then
    echo "Error: file not found: $tar_path"
    exit 1
  fi
  if [[ -f "${tar_path}.sha256" ]]; then
    echo "==> Verifying checksum …"
    sha256_check "${tar_path}.sha256"
  fi
  echo "==> Loading image from ${tar_path} (docker load) …"
  docker load -i "$tar_path"
  echo "==> Done. Run it with e.g.:"
  echo "    docker run -d --name ${CONTAINER} -p 127.0.0.1:5000:5000 -e RACK_STATUS_DEMO=1 -v rack-data:/app/var nvis-rack-status:latest"
  echo "    (HTTPS only: https://localhost:5000/ — a self-signed certificate is generated on first start)"
}

stop_container() {
  echo "==> Stopping ${CONTAINER} …"
  docker rm -f "$CONTAINER" 2>/dev/null || true
}

wait_ready() {
  local port="$1"
  echo "==> Waiting for the server …"
  # HTTPS only, and the certificate may be the self-signed one generated on first boot, hence -k:
  # bin/verify-deploy.sh verifies the chain properly. /api/about is public; /api/status is not.
  for _ in $(seq 1 30); do
    if curl -skf "https://localhost:${port}/api/about" >/dev/null 2>&1; then
      echo "==> Responding: https://localhost:${port}/"
      verify_deploy "$port"
      return $?
    fi
    sleep 1
  done
  echo "==> Container may still be starting. Logs:"
  docker logs --tail 30 "$CONTAINER"
  return 1
}

# A responding /api/status is NOT a working deployment. On 2026-08-12 the dashboard served a
# black screen for an hour with that check green: index.html referenced a bundle whose chunks
# were gone, so all 164 asset requests 404'd while the HTML served perfectly. bin/verify-deploy.sh
# checks what that missed — asset resolution, module imports, stale mounts, crash loops.
verify_deploy() {
  local port="$1"
  local script="$(dirname "$0")/bin/verify-deploy.sh"
  if [ ! -x "$script" ]; then
    echo "==> WARNING: $script missing; deployment is unverified."
    return 0
  fi
  echo "==> Verifying the deployment …"
  NVIS_CONTAINER="$CONTAINER" NVIS_VERIFY_PORT="$port" NVIS_DEPLOY_PATH="$(cd "$(dirname "$0")" && pwd)" \
    "$script" || {
      echo "==> Deployment did NOT verify. It is running but something above is broken."
      return 1
    }
}

run_dev() {
  local port="${RACK_WEB_PORT:-5050}"
  stop_container
  # Not "no auth" any more: the gate cannot be switched off, so this container bootstraps an
  # administrator and prints a one-time password to its log.
  echo "==> Starting in DEV mode (demo data, authenticated, HTTPS, port ${port}) …"
  docker run -d --name "$CONTAINER" -p "${RACK_WEB_BIND_ADDR:-127.0.0.1}:${port}:5000" \
    -e RACK_STATUS_DEMO=1 \
    -v rack-data:/app/var \
    "$IMAGE"
  wait_ready "$port"
}

run_prod() {
  local port="${RACK_WEB_PORT:-5000}"
  local ssh_key="${RACK_SSH_KEY_PATH:-$HOME/.ssh/id_rsa}"
  local config_dir
  config_dir="$(cd "$(dirname "$0")" && pwd)/config"

  # All settings live in config/rackstatus.toml, mounted read-only. Create the minimal one from
  # RACK_SSH_HOST on a first run; never overwrite an operator's file.
  if [[ ! -f "${config_dir}/rackstatus.toml" ]]; then
    if [[ -z "${RACK_SSH_HOST:-}" ]]; then
      echo "Error: ${config_dir}/rackstatus.toml does not exist. Create it with:"
      echo "  python3 backend/cli.py init-config --ssh-host <head-node>"
      echo "or run: RACK_SSH_HOST=<head-node> ./build-docker.sh prod"
      exit 1
    fi
    python3 "$(dirname "$0")/backend/cli.py" init-config --ssh-host "$RACK_SSH_HOST"
  fi
  # No credential to pass in. Shared-token and PAM login were removed (FR-18): the container
  # authenticates against its own identity store, and the FIRST start with an empty store prints a
  # one-time `admin` password to the container log. Capture it there:
  #
  #     docker logs CONTAINER 2>&1 | grep -A6 "ONE-TIME ADMINISTRATOR"
  #
  # It is shown once and only its verifier is kept, so losing it means resetting the credential
  # from another administrator's session — or redeploying the identity store if there is none.
  if [[ ! -f "$ssh_key" ]]; then
    echo "Warning: SSH key not found at ${ssh_key}"
  fi

  stop_container
  echo "==> Starting in PROD mode (HTTPS, port ${port}, config: ${config_dir}) …"
  # Loopback unless told otherwise, like docker-compose.yml: every access path is an ssh tunnel.
  # rack-data holds the identity store, certificate and history — without it a recreate loses all
  # three.
  docker run -d --name "$CONTAINER" -p "${RACK_WEB_BIND_ADDR:-127.0.0.1}:${port}:5000" \
    -v "${config_dir}:/app/config:ro" \
    -v "${ssh_key}:/run/secrets/ssh_key:ro" \
    -v rack-data:/app/var \
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
  save)
    save_image
    ;;
  load)
    load_image "${2:-}"
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
