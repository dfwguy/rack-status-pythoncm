#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/mockbin"
printf 'not-a-real-secret\n' >"$tmp/token"; chmod 600 "$tmp/token"
cat >"$tmp/mockbin/curl" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
url=''; data=''
for arg in "$@"; do
  [[ "$arg" == http* ]] && url=$arg
  [[ "$arg" == @* ]] && data=${arg#@}
done
case "$url" in
  */api/login) exit 0;;
  */api/validation-presets/prepare)
    jq -e '.preset_id=="exemplar-system-info" and .scope=="node" and .nodes==["node1","node2"]' "$data" >/dev/null
    if [[ "${MOCK_SCENARIO:-good}" == reject ]]; then printf '%s\n' '{"ok":false,"error":"ineligible"}';
    elif [[ "${MOCK_SCENARIO:-good}" == mismatch ]]; then printf '%s\n' '{"ok":true,"plan":{"plan_hash":"sealed123","nodes":["node1"],"checks":[{"ok":true}]}}';
    else printf '%s\n' '{"ok":true,"plan":{"plan_hash":"sealed123","nodes":["node1","node2"],"checks":["exact target reviewed",{"ok":true}]}}'; fi;;
  */api/run-validation-preset-async)
    jq -e '.plan_hash=="sealed123" and .nodes==["node1","node2"]' "$data" >/dev/null
    printf '%s\n' '{"ok":true,"job_id":"job-1","activity_id":"activity-1"}';;
  */api/run-validation-preset-status*)
    printf '%s\n' '{"ok":true,"phase":"done","returncode":0,"results_log_path":"/evidence/result.log"}';;
  *) printf 'unexpected URL: %s\n' "$url" >&2; exit 90;;
esac
MOCK
chmod 755 "$tmp/mockbin/curl"
cat >"$tmp/deploy.env" <<EOF
NVIS_VERIFY_URL=http://mock.invalid
RACK_WEB_AUTH_TOKEN_FILE=$tmp/token
NVIS_SMOKE_APPROVED=YES
NVIS_SMOKE_PRESET=exemplar-system-info
NVIS_SMOKE_SCOPE=node
NVIS_SMOKE_NODES=node1,node2
NVIS_SMOKE_TIMEOUT_SEC=5
NVIS_POLL_INTERVAL_SEC=1
EOF
chmod 600 "$tmp/deploy.env"

PATH="$tmp/mockbin:$PATH" NVIS_DEPLOY_ENV="$tmp/deploy.env" "$ROOT/scripts/launch-smoke.sh" | grep -q 'SMOKE_TERMINAL status=done'
if PATH="$tmp/mockbin:$PATH" MOCK_SCENARIO=reject NVIS_DEPLOY_ENV="$tmp/deploy.env" "$ROOT/scripts/launch-smoke.sh" >/dev/null 2>&1; then
  echo 'expected rejected plan to fail' >&2; exit 1
fi
if PATH="$tmp/mockbin:$PATH" MOCK_SCENARIO=mismatch NVIS_DEPLOY_ENV="$tmp/deploy.env" "$ROOT/scripts/launch-smoke.sh" >/dev/null 2>&1; then
  echo 'expected target mismatch to fail' >&2; exit 1
fi
printf 'PASS launch-smoke hermetic request/hash/target/rejection checks\n'
