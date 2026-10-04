#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# Idempotent, checkpointed Rack Status image deployment. Run on the app server.
#
# This repository is deployed air-gapped from a pre-built, CI-tested container IMAGE tarball:
# `docker load` the image, recreate the container with its config/state bind mounts, and prove the
# product works. There is no on-target source build and no source test run — the image is the
# artifact and was tested where it was built. Each phase has a durable checkpoint keyed by the
# image digest; a repeat run resumes rather than repeating, and any failure after the first
# mutation rolls back to the exact prior image.
set -Eeuo pipefail
umask 077
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/lib.sh"
NVIS_PHASE=bootstrap
nvis_load_env

image_sha=${NVIS_IMAGE_SHA256,,}
state_root=${NVIS_STATE_ROOT:-$NVIS_EVIDENCE_ROOT/state}
run_dir="$state_root/$image_sha"
checkpoint_dir="$run_dir/checkpoints"
NVIS_REPORT="$run_dir/deployment-report.txt"; export NVIS_REPORT
# shellcheck disable=SC2174  # umask 077 set above; intermediate dirs are already private
mkdir -p -m 700 "$checkpoint_dir"
touch "$NVIS_REPORT"; chmod 600 "$NVIS_REPORT"

phase_done() { [[ -f "$checkpoint_dir/$1.done" ]]; }
mark_done() { printf '%s\n' "$(date -u +%FT%TZ)" >"$checkpoint_dir/$1.done"; nvis_record "gate=$1 result=PASS"; }
run_phase() {
  local name=$1; shift
  NVIS_PHASE=$name; export NVIS_PHASE
  if phase_done "$name"; then nvis_record "gate=$name result=SKIPPED checkpoint=present"; return; fi
  "$@" >>"$NVIS_REPORT" 2>&1
  mark_done "$name"
}

rollback_state="$run_dir/rollback.env"
scripts_src="$run_dir/image-scripts"
mutation_started=0

on_error() {
  local rc=$?
  trap - ERR
  nvis_record "status=FAILED gate=$NVIS_PHASE exit=$rc"
  if (( mutation_started )); then
    NVIS_PHASE=rollback "$SCRIPT_DIR/rollback.sh" --state-dir "$run_dir" >>"$NVIS_REPORT" 2>&1 || nvis_record 'rollback=FAILED'
  fi
  printf 'BLOCKED gate=%s report=%s\n' "$NVIS_PHASE" "$NVIS_REPORT" >&2
  exit "$rc"
}
trap on_error ERR

do_preflight() { NVIS_DEPLOY_ENV="$NVIS_DEPLOY_ENV" "$SCRIPT_DIR/preflight.sh"; }

# Load the image tarball and confirm the loaded image is exactly the declared reference and the
# host's architecture — a cross-arch image loads cleanly and then fails only at runtime.
do_load() {
  docker load -i "$NVIS_IMAGE_TARBALL"
  docker image inspect "$NVIS_IMAGE_REF" >/dev/null 2>&1 || nvis_die "declared image not present after load: $NVIS_IMAGE_REF"
  local arch host_arch
  arch=$(docker image inspect "$NVIS_IMAGE_REF" --format '{{.Architecture}}')
  host_arch=$(docker info --format '{{.Architecture}}' 2>/dev/null || uname -m)
  case "$host_arch" in x86_64|amd64) host_arch=amd64;; aarch64|arm64) host_arch=arm64;; esac
  [[ "$arch" == "$host_arch" ]] || nvis_die "image architecture $arch does not match host $host_arch"
  docker image inspect "$NVIS_IMAGE_REF" --format '{{.Id}}' >"$run_dir/new-image-id"
}

# Refuse to deploy over a container whose on-disk tree has drifted from its image (someone edited
# it live), and confirm the head-node canonical script tree matches what this image carries.
do_drift() {
  if docker inspect "$NVIS_CONTAINER" >/dev/null 2>&1; then
    local diff
    diff=$(docker diff "$NVIS_CONTAINER" | grep -E '^C /app/(backend|bin|frontend|static)' || true)
    [[ -z "$diff" ]] || nvis_die "running container has live edits under /app; reconcile before deploying:"$'\n'"$diff"
  fi
  # Stage the image's own bin/scripts so drift and sync both compare against the artifact.
  local cid; cid=$(docker create "$NVIS_IMAGE_REF")
  trap 'docker rm -f "$cid" >/dev/null 2>&1 || true' RETURN
  rm -rf -- "$scripts_src"; mkdir -p "$scripts_src"
  docker cp "$cid:/app/bin/scripts/." "$scripts_src/" 2>/dev/null || nvis_die 'image has no /app/bin/scripts to deploy'
  nvis_ssh_opts
  local remote_dir="$RACK_REMOTE_BASE_PATH/bin/scripts"
  if ssh "${NVIS_SSH_OPTS[@]}" "$RACK_SSH_USER@$RACK_SSH_HOST" "test -d '$remote_dir'" </dev/null; then
    local expected actual; expected=$(mktemp); actual=$(mktemp)
    nvis_tree_manifest "$scripts_src" >"$expected"
    ssh "${NVIS_SSH_OPTS[@]}" "$RACK_SSH_USER@$RACK_SSH_HOST" \
      "cd '$remote_dir' && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum" </dev/null >"$actual"
    # Report-only on head drift: a mismatch means the head will be re-synced in do_sync_scripts,
    # which is a checksum-verified operation; record it as evidence rather than blocking.
    if ! diff -q <(awk '{print $1,$2}' "$expected") <(awk '{print $1,$2}' "$actual") >/dev/null; then
      nvis_record 'head_scripts=drift_detected will_resync=yes'
    fi
    rm -f "$expected" "$actual"
  else
    [[ "${NVIS_ALLOW_INITIAL_SCRIPT_INSTALL:-NO}" == YES ]] || nvis_die 'remote script tree is absent; set sealed NVIS_ALLOW_INITIAL_SCRIPT_INSTALL=YES for first install'
  fi
}

# Capture the exact prior container (image ref + id + the run spec) and a tar of the head scripts,
# so rollback is a deterministic recreate rather than a guess.
do_snapshot() {
  local old_ref='' old_id='' remote_existed=NO
  if docker inspect "$NVIS_CONTAINER" >/dev/null 2>&1; then
    old_ref=$(docker inspect -f '{{.Config.Image}}' "$NVIS_CONTAINER")
    old_id=$(docker inspect -f '{{.Image}}' "$NVIS_CONTAINER")
    docker inspect "$NVIS_CONTAINER" >"$run_dir/old-container.json"
  fi
  nvis_ssh_opts
  if ssh "${NVIS_SSH_OPTS[@]}" "$RACK_SSH_USER@$RACK_SSH_HOST" "test -d '$RACK_REMOTE_BASE_PATH/bin/scripts'" </dev/null; then
    remote_existed=YES
    ssh "${NVIS_SSH_OPTS[@]}" "$RACK_SSH_USER@$RACK_SSH_HOST" \
      "cd '$RACK_REMOTE_BASE_PATH' && tar -cf - bin/scripts" </dev/null >"$run_dir/remote-scripts-before.tar"
    sha256sum "$run_dir/remote-scripts-before.tar" >"$run_dir/remote-scripts-before.tar.sha256"
  fi
  printf 'NVIS_OLD_IMAGE_REF=%q\nNVIS_OLD_IMAGE_ID=%q\nNVIS_NEW_IMAGE_REF=%q\nNVIS_CONTAINER=%q\nNVIS_REMOTE_SCRIPTS_EXISTED=%q\n' \
    "$old_ref" "$old_id" "$NVIS_IMAGE_REF" "$NVIS_CONTAINER" "$remote_existed" >"$rollback_state"
  chmod 600 "$rollback_state"
  nvis_record "rollback_image_ref=${old_ref:-none} rollback_image_id=${old_id:-none}"
}

# Checksum-verified sync of the image's bin/scripts to the head, staged then swapped, never
# deleting unrelated head-only files. Pinned host key, non-root identity.
do_sync_scripts() {
  mutation_started=1
  local manifest="$run_dir/scripts.manifest"
  nvis_tree_manifest "$scripts_src" >"$manifest"
  nvis_ssh_opts
  tar -C "$scripts_src" -cf - . | ssh "${NVIS_SSH_OPTS[@]}" "$RACK_SSH_USER@$RACK_SSH_HOST" \
    "set -eu; base='$RACK_REMOTE_BASE_PATH'; tmp=\"\$base/.scripts-stage-$image_sha\"; rm -rf \"\$tmp\"; mkdir -p \"\$tmp\" \"\$base/bin/scripts\"; tar -C \"\$tmp\" -xf -; cp -a \"\$tmp/.\" \"\$base/bin/scripts/\"; rm -rf \"\$tmp\""
  while IFS= read -r line; do
    local sha rel got; sha=${line%% *}; rel=${line#*  }
    [[ "$rel" =~ ^\./[A-Za-z0-9._/-]+$ ]] || nvis_die 'unsafe script manifest path'
    got=$(ssh "${NVIS_SSH_OPTS[@]}" "$RACK_SSH_USER@$RACK_SSH_HOST" "sha256sum '$RACK_REMOTE_BASE_PATH/bin/scripts/${rel#./}'" </dev/null | awk '{print $1}')
    [[ "$got" == "$sha" ]] || nvis_die "remote script checksum mismatch: $rel"
  done <"$manifest"
}

# Recreate the container from the new image with the site's config/state bind mounts. Identical
# shape to the documented `docker run`, generated from the sealed environment so it cannot drift.
do_deploy() {
  mutation_started=1
  docker rm -f "$NVIS_CONTAINER" >/dev/null 2>&1 || true
  local args=(run -d --name "$NVIS_CONTAINER" --restart unless-stopped
    -p "${NVIS_BIND_ADDR:-127.0.0.1}:${NVIS_WEB_PORT}:5000"
    -v "$NVIS_CONFIG_DIR:/app/config:ro"
    -v "$NVIS_STATE_DIR:/app/var"
    -v "$RACK_SSH_KEY_PATH:/run/secrets/ssh_key:ro"
    -e RACK_SSH_KEY=/run/secrets/ssh_key)
  [[ -n "${NVIS_CPUS:-}" ]]   && args+=(--cpus "$NVIS_CPUS")
  [[ -n "${NVIS_MEMORY:-}" ]] && args+=(--memory "$NVIS_MEMORY")
  [[ -n "${NVIS_CA_CERT_FILE:-}" ]] && args+=(-v "$NVIS_CA_CERT_FILE:$NVIS_CA_CERT_FILE:ro")
  args+=("$NVIS_IMAGE_REF")
  docker "${args[@]}"
  # Pin the running container to the exact image we loaded, not a tag that may be repointed later.
  local running; running=$(docker inspect -f '{{.Image}}' "$NVIS_CONTAINER")
  [[ "$running" == "$(<"$run_dir/new-image-id")" ]] || nvis_die "running image $running is not the loaded image"
}

do_verify() { "$SCRIPT_DIR/verify.sh"; }

do_activate() {
  local link="$NVIS_EVIDENCE_ROOT/current-image"
  printf '%s\n' "$NVIS_IMAGE_REF $(<"$run_dir/new-image-id")" >"$link"
  nvis_record "image_ref=$NVIS_IMAGE_REF image_id=$(<"$run_dir/new-image-id") config_dir=$NVIS_CONFIG_DIR state_dir=$NVIS_STATE_DIR"
}

do_optional_smoke() {
  if [[ "$NVIS_SMOKE_APPROVED" == YES ]]; then "$SCRIPT_DIR/launch-smoke.sh"; else printf 'smoke=NOT_AUTHORIZED\n'; fi
}

run_phase preflight do_preflight
run_phase load do_load
run_phase drift do_drift
run_phase snapshot do_snapshot
run_phase scripts do_sync_scripts
run_phase deploy do_deploy
mutation_started=1
run_phase verify do_verify
run_phase activate do_activate
run_phase smoke do_optional_smoke
mutation_started=0
nvis_record 'status=DEPLOYED'
printf 'DEPLOYED image=%s container=%s report=%s\n' "$NVIS_IMAGE_REF" "$NVIS_CONTAINER" "$NVIS_REPORT"
