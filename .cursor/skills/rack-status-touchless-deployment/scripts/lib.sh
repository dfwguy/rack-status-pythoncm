#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# Shared, non-mutating helpers for the Rack Status touchless deployment skill.
#
# Adapted for THIS repository's deployment model, which differs from the upstream
# Agentic-Harness skill in two load-bearing ways:
#   * We ship a pre-built, pre-tested container IMAGE tarball (air-gapped: the image is built and
#     tested off-box in CI, then `docker load`ed here) — not a repository tarball built on target.
#   * Authentication is SRP identity (an administrator the store bootstraps), not a shared web
#     token. There is therefore no token file to mount and no cookie login to perform; the
#     deployment gate verifies the product unauthenticated via `bin/verify-deploy.sh`.

# One allowlisted key per line in the sealed environment file. Secrets stay in separately
# protected files named by these variables; the parser never evaluates a value.
NVIS_ALLOWED_KEYS=' NVIS_IMAGE_TARBALL NVIS_IMAGE_SHA256 NVIS_IMAGE_REF NVIS_DEPLOY_HOST NVIS_CONTAINER NVIS_CONFIG_DIR NVIS_STATE_DIR NVIS_WEB_PORT NVIS_BIND_ADDR NVIS_VERIFY_URL NVIS_VERIFY_PATH NVIS_EVIDENCE_ROOT NVIS_STATE_ROOT NVIS_CPUS NVIS_MEMORY NVIS_SITE_PROFILE NVIS_CA_CERT_FILE NVIS_SMOKE_APPROVED NVIS_SMOKE_PRESET NVIS_SMOKE_SCOPE NVIS_SMOKE_NODES NVIS_SMOKE_TIMEOUT_SEC NVIS_POLL_INTERVAL_SEC NVIS_ALLOW_INITIAL_SCRIPT_INSTALL RACK_SSH_HOST RACK_SSH_PORT RACK_SSH_USER RACK_SSH_KEY_PATH RACK_KNOWN_HOSTS_PATH RACK_REMOTE_BASE_PATH RACK_BCM_CREDENTIALS_PATH RACK_SCHEDULER '

nvis_die() { printf 'BLOCKED %s: %s\n' "${NVIS_PHASE:-deployment}" "$*" >&2; exit 2; }
nvis_need() { command -v "$1" >/dev/null 2>&1 || nvis_die "missing command: $1"; }
nvis_need_var() { [[ -n "${!1:-}" ]] || nvis_die "missing required variable: $1"; }

# Parse the sealed environment file. Deliberately narrower than shell: one UPPER_CASE=value per
# line, optional whole-value quotes, comments on their own line. No export, interpolation, command
# substitution, or multiline values — the values are never evaluated.
nvis_load_env() {
  local file="${1:-${NVIS_DEPLOY_ENV:-}}" line key value lineno=0
  [[ -n "$file" && "$file" = /* && -f "$file" && ! -L "$file" ]] ||
    nvis_die 'NVIS_DEPLOY_ENV must name an absolute regular non-symlink file'
  local perm owner
  perm=$(stat -c '%a' "$file")
  owner=$(stat -c '%u' "$file")
  [[ "$perm" == 600 || "$perm" == 400 ]] || nvis_die "deployment environment permissions are $perm; require 600 or 400"
  [[ "$owner" == "$(id -u)" ]] || nvis_die 'deployment environment must be owned by the deployment identity'
  while IFS= read -r line || [[ -n "$line" ]]; do
    lineno=$((lineno + 1)); line=${line%$'\r'}
    [[ "$line" =~ ^[[:space:]]*$ || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]] || nvis_die "invalid environment syntax at line $lineno"
    key=${BASH_REMATCH[1]}; value=${BASH_REMATCH[2]}
    [[ "$NVIS_ALLOWED_KEYS" == *" $key "* ]] || nvis_die "unsupported environment key at line $lineno: $key"
    if [[ "$value" =~ ^\'(.*)\'$ || "$value" =~ ^\"(.*)\"$ ]]; then value=${BASH_REMATCH[1]}; fi
    [[ "$value" != *$'\n'* ]] || nvis_die "invalid value at line $lineno"
    printf -v "$key" '%s' "$value"
    # shellcheck disable=SC2163  # dynamic export of the allowlisted key named by $key
    export "$key"
  done < "$file"
  NVIS_DEPLOY_ENV=$file; export NVIS_DEPLOY_ENV
  if [[ -n "${NVIS_CA_CERT_FILE:-}" ]]; then CURL_CA_BUNDLE=$NVIS_CA_CERT_FILE; export CURL_CA_BUNDLE; fi
}

nvis_safe_abs_path() {
  [[ "$1" = /* && "$1" != / && "$1" != *'/../'* && "$1" != */.. ]]
}

nvis_validate_safe_remote_path() {
  [[ "$1" =~ ^/[A-Za-z0-9._/-]+$ && "$1" != / && "$1" != *'/../'* && "$1" != */.. ]]
}

# SSH to the head node with a PINNED known-hosts file and strict host-key checking, never with
# host-key verification disabled. The head-node identity is non-root and least-privileged.
nvis_ssh_opts() {
  NVIS_SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$RACK_KNOWN_HOSTS_PATH" -i "$RACK_SSH_KEY_PATH")
  [[ -n "${RACK_SSH_PORT:-}" ]] && NVIS_SSH_OPTS+=(-p "$RACK_SSH_PORT")
  return 0
}

# A read-only reachability probe of the running product. SRP auth means no credential is needed:
# /api/status answers unauthenticated and reports the real health, and bin/verify-deploy.sh does
# the deep check. This is only the liveness ping used while polling a recreate.
nvis_health_ping() {
  local curl_tls=()
  [[ -n "${NVIS_CA_CERT_FILE:-}" ]] && curl_tls=(--cacert "$NVIS_CA_CERT_FILE")
  curl --fail --silent --show-error --connect-timeout 5 --max-time 20 \
    "${curl_tls[@]}" "${NVIS_VERIFY_URL%/}/api/status" >/dev/null
}

# Deterministic checksum manifest of a tree, for drift detection and rollback attestation.
nvis_tree_manifest() {
  local root=$1
  (cd "$root" && find . \
    \( -name .git -o -name node_modules -o -name __pycache__ -o -name .venv \) -prune -o \
    -type f ! -name .nvis-release-manifest -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum)
}

nvis_record() {
  [[ -n "${NVIS_REPORT:-}" ]] || return 0
  printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >>"$NVIS_REPORT"
}
