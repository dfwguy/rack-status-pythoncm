#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cat >"$tmp/good.env" <<'EOF'
NVIS_COMPOSE_PROJECT=nvis-rack-status
NVIS_SMOKE_APPROVED=NO
EOF
chmod 600 "$tmp/good.env"
NVIS_DEPLOY_ENV="$tmp/good.env" bash -c 'source "$1/scripts/lib.sh"; NVIS_PHASE=test; nvis_load_env; [[ "$NVIS_COMPOSE_PROJECT" == nvis-rack-status ]]' _ "$ROOT"
cat >"$tmp/bad.env" <<'EOF'
NVIS_COMPOSE_PROJECT=$(touch /tmp/must-not-exist)
UNREVIEWED_VARIABLE=value
EOF
chmod 600 "$tmp/bad.env"
if NVIS_DEPLOY_ENV="$tmp/bad.env" bash -c 'source "$1/scripts/lib.sh"; NVIS_PHASE=test; nvis_load_env' _ "$ROOT" >/dev/null 2>&1; then
  echo 'expected unknown key rejection' >&2; exit 1
fi
[[ ! -e /tmp/must-not-exist ]] || { echo 'environment parser executed input' >&2; exit 1; }
printf 'PASS strict non-evaluating environment parser checks\n'
