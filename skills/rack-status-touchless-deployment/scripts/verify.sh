#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/lib.sh"
NVIS_PHASE=verify
nvis_load_env
release_root=${NVIS_RELEASE_ROOT:-}
while [[ $# -gt 0 ]]; do
  case "$1" in --release-root) release_root=${2:?}; shift 2;; *) nvis_die "unknown argument: $1";; esac
done
for var in NVIS_CONTAINER NVIS_VERIFY_URL RACK_WEB_AUTH_TOKEN_FILE NVIS_DEPLOY_HOST; do nvis_need_var "$var"; done
[[ -n "$release_root" && -d "$release_root" ]] || nvis_die 'release root is absent'
grep -Fqx "$NVIS_DEPLOY_HOST" < <(printf '%s\n%s\n%s\n' "$(hostname)" "$(hostname -s)" "$(hostname -f 2>/dev/null || true)") || nvis_die 'verification is running on a host other than NVIS_DEPLOY_HOST'

cd "$release_root"
[[ -x bin/verify-deploy.sh ]] || nvis_die 'repository verify-deploy.sh absent'
NVIS_DEPLOY_SSH='' NVIS_DEPLOY_PATH="$release_root" NVIS_CONTAINER="$NVIS_CONTAINER" \
  NVIS_VERIFY_PORT="${NVIS_VERIFY_PORT:-}" bin/verify-deploy.sh || nvis_die 'repository deployment gate failed'

container_id=$(docker inspect -f '{{.Id}}' "$NVIS_CONTAINER" 2>/dev/null) || nvis_die 'deployed container absent'
image_id=$(docker inspect -f '{{.Image}}' "$NVIS_CONTAINER")
image_ref=$(docker inspect -f '{{.Config.Image}}' "$NVIS_CONTAINER")
build_id=$(docker exec "$NVIS_CONTAINER" python3 -c \
  'import json; print(json.load(open("/app/backend/build-info.json"))["build"])' 2>/dev/null) || nvis_die 'cannot attest build ID'

cookie_file=$(mktemp); login_file=$(mktemp)
trap 'rm -f "$cookie_file" "$login_file"' EXIT
nvis_cookie_login "$cookie_file" "$login_file"
for endpoint in /api/status /api/validation-activities /api/post-run-node-report /api/reports/generated /api/metrics/health; do
  curl --fail --silent --show-error --connect-timeout 5 --max-time 30 -b "$cookie_file" \
    "${NVIS_VERIFY_URL%/}${endpoint}" >/dev/null || nvis_die "API failed: $endpoint"
done
printf 'VERIFY_OK host=%s container=%s container_id=%s image_ref=%s image_id=%s build_id=%s url=%s\n' \
  "$NVIS_DEPLOY_HOST" "$NVIS_CONTAINER" "$container_id" "$image_ref" "$image_id" "$build_id" "$NVIS_VERIFY_URL"
