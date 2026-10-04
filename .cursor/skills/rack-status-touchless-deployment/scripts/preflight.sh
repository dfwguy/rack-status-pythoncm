#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# Gate 1-2: release-image safety and environment readiness. Changes nothing.
set -euo pipefail
umask 077
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/lib.sh"
# shellcheck disable=SC2034  # read by nvis_die() in lib.sh
NVIS_PHASE=preflight
nvis_load_env

for cmd in bash sha256sum tar ssh docker curl python3 find xargs; do nvis_need "$cmd"; done
for var in NVIS_IMAGE_TARBALL NVIS_IMAGE_SHA256 NVIS_IMAGE_REF NVIS_DEPLOY_HOST NVIS_CONTAINER \
           NVIS_CONFIG_DIR NVIS_STATE_DIR NVIS_WEB_PORT NVIS_VERIFY_URL NVIS_EVIDENCE_ROOT \
           RACK_SSH_HOST RACK_SSH_USER RACK_SSH_KEY_PATH RACK_KNOWN_HOSTS_PATH RACK_REMOTE_BASE_PATH \
           RACK_BCM_CREDENTIALS_PATH RACK_SCHEDULER NVIS_SMOKE_APPROVED; do nvis_need_var "$var"; done

# --- the release image: an exact, pre-built, checksum-pinned artifact ---
[[ "$NVIS_IMAGE_SHA256" =~ ^[0-9a-fA-F]{64}$ ]] || nvis_die 'NVIS_IMAGE_SHA256 must be exactly 64 hexadecimal characters'
[[ -f "$NVIS_IMAGE_TARBALL" && ! -L "$NVIS_IMAGE_TARBALL" ]] || nvis_die "image tarball not found or is a symlink: $NVIS_IMAGE_TARBALL"
printf '%s  %s\n' "${NVIS_IMAGE_SHA256,,}" "$NVIS_IMAGE_TARBALL" | sha256sum -c - >/dev/null || nvis_die 'image tarball checksum mismatch'
[[ "$NVIS_IMAGE_REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*:[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || nvis_die 'NVIS_IMAGE_REF must be repo:tag'

# --- identities, names, and paths ---
nvis_safe_abs_path "$NVIS_CONFIG_DIR" || nvis_die 'unsafe NVIS_CONFIG_DIR'
nvis_safe_abs_path "$NVIS_STATE_DIR"  || nvis_die 'unsafe NVIS_STATE_DIR'
nvis_safe_abs_path "$NVIS_EVIDENCE_ROOT" || nvis_die 'unsafe NVIS_EVIDENCE_ROOT'
nvis_validate_safe_remote_path "$RACK_REMOTE_BASE_PATH" || nvis_die 'unsafe RACK_REMOTE_BASE_PATH'
[[ "$RACK_SSH_USER" =~ ^[a-z_][a-z0-9_-]*$ && "$RACK_SSH_USER" != root ]] || nvis_die 'head-node SSH identity must be a safe non-root account'
[[ "$RACK_SSH_HOST" =~ ^[A-Za-z0-9._:-]+$ ]] || nvis_die 'invalid head-node host'
[[ "$NVIS_CONTAINER" =~ ^[A-Za-z0-9_.-]+$ ]] || nvis_die 'invalid container name'
[[ "$NVIS_WEB_PORT" =~ ^[0-9]{2,5}$ ]] || nvis_die 'invalid web port'
[[ "$NVIS_VERIFY_URL" =~ ^http://(127\.0\.0\.1|localhost)(:[0-9]+)?$ || "$NVIS_VERIFY_URL" =~ ^https://[A-Za-z0-9._:-]+$ ]] || nvis_die 'verification URL must be loopback HTTP or HTTPS'
[[ "$RACK_SCHEDULER" =~ ^(pythoncm|slurm)$ ]] || nvis_die 'RACK_SCHEDULER must be pythoncm or slurm'
[[ "$NVIS_SMOKE_APPROVED" == YES || "$NVIS_SMOKE_APPROVED" == NO ]] || nvis_die 'NVIS_SMOKE_APPROVED must be YES or NO'
if [[ "$NVIS_SMOKE_APPROVED" == YES ]]; then
  for var in NVIS_SMOKE_PRESET NVIS_SMOKE_SCOPE NVIS_SMOKE_NODES; do nvis_need_var "$var"; done
  [[ "$NVIS_SMOKE_NODES" =~ ^[A-Za-z0-9._-]+(,[A-Za-z0-9._-]+)*$ ]] || nvis_die 'smoke nodes must be an exact comma-separated list'
fi

# --- run on the declared app server, never somewhere else ---
local_names=$(printf '%s\n%s\n%s\n' "$(hostname)" "$(hostname -s)" "$(hostname -f 2>/dev/null || true)")
grep -Fqx "$NVIS_DEPLOY_HOST" <<<"$local_names" || nvis_die "run this on the declared app server ($NVIS_DEPLOY_HOST), not $(hostname -f 2>/dev/null || hostname)"

# --- protected files/dirs: present, absolute, not symlinked, not group/world writable ---
for path in "$RACK_SSH_KEY_PATH" "$RACK_KNOWN_HOSTS_PATH"; do
  [[ "$path" = /* && -f "$path" && ! -L "$path" ]] || nvis_die "required protected file absent, relative, or symlinked: $path"
  [[ -r "$path" ]] || nvis_die "protected file is not readable by deployment identity: $path"
  p=$(stat -c '%a' "$path"); (( (8#$p & 8#022) == 0 )) || nvis_die "protected file is group/world writable: $path"
done
# A pinned known-hosts file with no host keys would silently accept any host on first use.
(( $(grep -cv '^[[:space:]]*#\|^[[:space:]]*$' "$RACK_KNOWN_HOSTS_PATH") > 0 )) || nvis_die 'pinned known-hosts file has no host keys'
[[ -d "$RACK_BCM_CREDENTIALS_PATH" && ! -L "$RACK_BCM_CREDENTIALS_PATH" ]] || nvis_die "BCM credential directory absent or symlinked: $RACK_BCM_CREDENTIALS_PATH"
bp=$(stat -c '%a' "$RACK_BCM_CREDENTIALS_PATH"); (( (8#$bp & 8#022) == 0 )) || nvis_die 'BCM credential directory is group/world writable'
[[ -d "$NVIS_CONFIG_DIR" ]] || nvis_die "config directory absent: $NVIS_CONFIG_DIR"
[[ -f "$NVIS_CONFIG_DIR/rackstatus.toml" ]] || nvis_die "config directory has no rackstatus.toml: $NVIS_CONFIG_DIR"
if [[ -n "${NVIS_CA_CERT_FILE:-}" ]]; then
  [[ "$NVIS_CA_CERT_FILE" = /* && -f "$NVIS_CA_CERT_FILE" && ! -L "$NVIS_CA_CERT_FILE" ]] || nvis_die 'CA certificate file is absent, relative, or symlinked'
fi

# --- Docker WITHOUT sudo, and pinned-host-key head reachability ---
docker info >/dev/null 2>&1 || nvis_die 'direct Docker socket access failed; the deployment identity must reach Docker without elevation'
nvis_ssh_opts
ssh "${NVIS_SSH_OPTS[@]}" "$RACK_SSH_USER@$RACK_SSH_HOST" \
  'command -v /cm/local/apps/cmd/bin/cmsh >/dev/null || command -v python3 >/dev/null' </dev/null ||
  nvis_die 'noninteractive pinned-host-key access or BCM tooling availability failed'

# --- deployment-owned writable locations ---
# shellcheck disable=SC2174  # umask 077 is set; intermediate dirs are already private
mkdir -p -m 700 "$NVIS_EVIDENCE_ROOT" "${NVIS_STATE_ROOT:-$NVIS_EVIDENCE_ROOT/state}"
[[ -w "$NVIS_STATE_DIR" ]] || { mkdir -p "$NVIS_STATE_DIR" 2>/dev/null || nvis_die "state directory is not writable: $NVIS_STATE_DIR"; }

printf 'PREFLIGHT_OK image_sha256=%s image_ref=%s app_host=%s container=%s head_user=%s scheduler=%s smoke=%s\n' \
  "${NVIS_IMAGE_SHA256,,}" "$NVIS_IMAGE_REF" "$NVIS_DEPLOY_HOST" "$NVIS_CONTAINER" "$RACK_SSH_USER" "$RACK_SCHEDULER" "$NVIS_SMOKE_APPROVED"
