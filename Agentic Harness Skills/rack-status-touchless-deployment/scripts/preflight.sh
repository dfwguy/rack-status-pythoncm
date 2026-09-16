#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/lib.sh"
NVIS_PHASE=preflight
nvis_load_env

for cmd in bash git sha256sum tar ssh docker curl jq python3 find xargs rg; do nvis_need "$cmd"; done
for var in NVIS_RELEASE_TARBALL NVIS_RELEASE_SHA256 NVIS_DEPLOY_PATH NVIS_DEPLOY_HOST NVIS_COMPOSE_PROJECT NVIS_CONTAINER NVIS_COMPOSE_SERVICE NVIS_VERIFY_URL NVIS_SITE_PROFILE NVIS_EVIDENCE_ROOT RACK_SSH_HOST RACK_SSH_USER RACK_SSH_KEY_PATH RACK_KNOWN_HOSTS_PATH RACK_REMOTE_BASE_PATH RACK_WEB_AUTH_TOKEN_FILE RACK_BCM_CREDENTIALS_PATH RACK_SCHEDULER RACK_VALIDATION_CONTAINER_ROOT NVIS_SMOKE_APPROVED; do nvis_need_var "$var"; done

[[ "$NVIS_RELEASE_SHA256" =~ ^[0-9a-fA-F]{64}$ ]] || nvis_die 'NVIS_RELEASE_SHA256 must be exactly 64 hexadecimal characters'
[[ -f "$NVIS_RELEASE_TARBALL" && ! -L "$NVIS_RELEASE_TARBALL" ]] || nvis_die "release not found or is a symlink: $NVIS_RELEASE_TARBALL"
printf '%s  %s\n' "${NVIS_RELEASE_SHA256,,}" "$NVIS_RELEASE_TARBALL" | sha256sum -c - >/dev/null || nvis_die 'release checksum mismatch'
nvis_safe_abs_path "$NVIS_DEPLOY_PATH" || nvis_die 'unsafe NVIS_DEPLOY_PATH'
nvis_safe_abs_path "$NVIS_EVIDENCE_ROOT" || nvis_die 'unsafe NVIS_EVIDENCE_ROOT'
nvis_validate_safe_remote_path "$RACK_REMOTE_BASE_PATH" || nvis_die 'unsafe RACK_REMOTE_BASE_PATH'
[[ "$RACK_SSH_USER" =~ ^[a-z_][a-z0-9_-]*$ && "$RACK_SSH_USER" != root ]] || nvis_die 'head-node SSH identity must be a safe non-root account'
[[ "$RACK_SSH_HOST" =~ ^[A-Za-z0-9._:-]+$ ]] || nvis_die 'invalid head-node host'
[[ "$NVIS_COMPOSE_PROJECT" =~ ^[a-z0-9][a-z0-9_-]+$ ]] || nvis_die 'invalid Compose project'
[[ "$NVIS_COMPOSE_SERVICE" =~ ^[A-Za-z0-9_.-]+$ && "$NVIS_CONTAINER" =~ ^[A-Za-z0-9_.-]+$ ]] || nvis_die 'invalid service/container name'
[[ "$NVIS_VERIFY_URL" =~ ^http://(127\.0\.0\.1|localhost)(:[0-9]+)?$ || "$NVIS_VERIFY_URL" =~ ^https://[A-Za-z0-9._:-]+$ ]] || nvis_die 'verification URL must be loopback HTTP or HTTPS'
[[ "$RACK_SCHEDULER" == pythoncm ]] || nvis_die 'touchless deployment requires RACK_SCHEDULER=pythoncm'
[[ "$NVIS_SMOKE_APPROVED" == YES || "$NVIS_SMOKE_APPROVED" == NO ]] || nvis_die 'NVIS_SMOKE_APPROVED must be YES or NO'
if [[ "$NVIS_SMOKE_APPROVED" == YES ]]; then
  for var in NVIS_SMOKE_PRESET NVIS_SMOKE_SCOPE NVIS_SMOKE_NODES; do nvis_need_var "$var"; done
  [[ "$NVIS_SMOKE_NODES" =~ ^[A-Za-z0-9._-]+(,[A-Za-z0-9._-]+)*$ ]] || nvis_die 'smoke nodes must be an exact comma-separated list'
fi
[[ "${NVIS_STAGE_APPROVED:-NO}" == YES || "${NVIS_STAGE_APPROVED:-NO}" == NO ]] || nvis_die 'NVIS_STAGE_APPROVED must be YES or NO'
[[ "${NVIS_STAGE_ONLY:-false}" == true || "${NVIS_STAGE_ONLY:-false}" == false ]] || nvis_die 'NVIS_STAGE_ONLY must be true or false'
if [[ "${NVIS_STAGE_ONLY:-false}" == true ]]; then
  [[ "${NVIS_STAGE_APPROVED:-NO}" == YES && "$NVIS_SMOKE_APPROVED" == YES ]] || nvis_die 'stage-only mutation requires NVIS_STAGE_APPROVED=YES and smoke approval'
  [[ "$NVIS_SMOKE_PRESET" == mnubergemm-26.03-v1.17 ]] || nvis_die 'the reviewed stage-only workflow supports only the MNUberGEMM preset'
fi

local_names=$(printf '%s\n%s\n%s\n' "$(hostname)" "$(hostname -s)" "$(hostname -f 2>/dev/null || true)")
grep -Fqx "$NVIS_DEPLOY_HOST" <<<"$local_names" || nvis_die "run this script on the declared app server ($NVIS_DEPLOY_HOST), not $(hostname -f 2>/dev/null || hostname)"

for path in "$RACK_SSH_KEY_PATH" "$RACK_KNOWN_HOSTS_PATH" "$RACK_WEB_AUTH_TOKEN_FILE"; do
  [[ "$path" = /* && -f "$path" && ! -L "$path" ]] || nvis_die "required protected file absent, relative, or symlinked: $path"
  [[ -r "$path" ]] || nvis_die "protected file is not readable by deployment identity: $path"
  p=$(stat -c '%a' "$path"); (( (8#$p & 8#022) == 0 )) || nvis_die "protected file is group/world writable: $path"
done
if [[ -n "${NVIS_CA_CERT_FILE:-}" ]]; then
  [[ "$NVIS_CA_CERT_FILE" = /* && -f "$NVIS_CA_CERT_FILE" && ! -L "$NVIS_CA_CERT_FILE" ]] || nvis_die 'CA certificate file is absent, relative, or symlinked'
fi
[[ -d "$RACK_BCM_CREDENTIALS_PATH" && ! -L "$RACK_BCM_CREDENTIALS_PATH" ]] || nvis_die "BCM credential directory absent or symlinked: $RACK_BCM_CREDENTIALS_PATH"
bp=$(stat -c '%a' "$RACK_BCM_CREDENTIALS_PATH"); (( (8#$bp & 8#022) == 0 )) || nvis_die 'BCM credential directory is group/world writable'
[[ -d "$RACK_VALIDATION_CONTAINER_ROOT" ]] || nvis_die "validation container root absent: $RACK_VALIDATION_CONTAINER_ROOT"
(( $(wc -c < "$RACK_WEB_AUTH_TOKEN_FILE") > 1 )) || nvis_die 'web token file is empty'

docker info >/dev/null 2>&1 || nvis_die 'direct Docker access failed; never use sudo docker'
docker compose version >/dev/null 2>&1 || nvis_die 'Docker Compose v2 unavailable'
nvis_ssh_opts
ssh "${NVIS_SSH_OPTS[@]}" "$RACK_SSH_USER@$RACK_SSH_HOST" \
  'command -v /cm/local/apps/cmd/pythoncm >/dev/null || command -v python3 >/dev/null' </dev/null ||
  nvis_die 'noninteractive pinned-host-key access or BCM Python availability failed'

mkdir -p -m 700 "$NVIS_EVIDENCE_ROOT" "${NVIS_STATE_ROOT:-$NVIS_EVIDENCE_ROOT/state}"
test_file="$NVIS_DEPLOY_PATH/.nvis-write-test.$$"
mkdir -p "$NVIS_DEPLOY_PATH" && : >"$test_file" && rm -f "$test_file" || nvis_die 'deployment path is not writable'
printf 'PREFLIGHT_OK release_sha256=%s app_host=%s project=%s container=%s head_user=%s smoke=%s\n' \
  "${NVIS_RELEASE_SHA256,,}" "$NVIS_DEPLOY_HOST" "$NVIS_COMPOSE_PROJECT" "$NVIS_CONTAINER" "$RACK_SSH_USER" "$NVIS_SMOKE_APPROVED"
