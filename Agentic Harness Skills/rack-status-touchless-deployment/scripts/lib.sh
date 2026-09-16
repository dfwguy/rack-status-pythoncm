#!/usr/bin/env bash
# Shared, non-mutating helpers for the Rack Status deployment skill.

NVIS_ALLOWED_KEYS=' NVIS_RELEASE_TARBALL NVIS_RELEASE_SHA256 NVIS_DEPLOY_PATH NVIS_DEPLOY_HOST NVIS_COMPOSE_PROJECT NVIS_COMPOSE_SERVICE NVIS_CONTAINER NVIS_VERIFY_URL NVIS_VERIFY_PORT NVIS_SITE_PROFILE NVIS_EVIDENCE_ROOT NVIS_STATE_ROOT NVIS_ARCHIVE_MAX_FILES NVIS_ARCHIVE_MAX_BYTES NVIS_PYTHON_BIN NVIS_NPM_BIN NVIS_COMPOSE_OVERRIDE NVIS_CA_CERT_FILE NVIS_STAGE_APPROVED NVIS_STAGE_ONLY NVIS_SMOKE_APPROVED NVIS_SMOKE_PRESET NVIS_SMOKE_SCOPE NVIS_SMOKE_NODES NVIS_SMOKE_TIMEOUT_SEC NVIS_POLL_INTERVAL_SEC NVIS_ALLOW_INITIAL_SCRIPT_INSTALL RACK_SSH_HOST RACK_SSH_PORT RACK_SSH_USER RACK_SSH_KEY_PATH RACK_KNOWN_HOSTS_PATH RACK_REMOTE_BASE_PATH RACK_WEB_AUTH_TOKEN_FILE RACK_BCM_CREDENTIALS_PATH RACK_PUNCHCARD_TOKEN_PATH RACK_SCHEDULER RACK_VALIDATION_CONTAINER_ROOT '

nvis_die() { printf 'BLOCKED %s: %s\n' "${NVIS_PHASE:-deployment}" "$*" >&2; exit 2; }
nvis_need() { command -v "$1" >/dev/null 2>&1 || nvis_die "missing command: $1"; }
nvis_need_var() { [[ -n "${!1:-}" ]] || nvis_die "missing required variable: $1"; }

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

nvis_ssh_opts() {
  NVIS_SSH_OPTS=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$RACK_KNOWN_HOSTS_PATH" -i "$RACK_SSH_KEY_PATH")
  [[ -n "${RACK_SSH_PORT:-}" ]] && NVIS_SSH_OPTS+=(-p "$RACK_SSH_PORT")
  return 0
}

nvis_cookie_login() {
  local cookie=$1 login=$2
  local curl_tls=()
  [[ -n "${NVIS_CA_CERT_FILE:-}" ]] && curl_tls=(--cacert "$NVIS_CA_CERT_FILE")
  tr -d '\r\n' <"$RACK_WEB_AUTH_TOKEN_FILE" | jq -Rs '{password:.}' >"$login"
  chmod 600 "$cookie" "$login"
  curl --fail --silent --show-error --connect-timeout 5 --max-time 30 \
    "${curl_tls[@]}" \
    -c "$cookie" -H 'Content-Type: application/json' --data-binary @"$login" \
    "${NVIS_VERIFY_URL%/}/api/login" >/dev/null || nvis_die 'web login failed'
}

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
