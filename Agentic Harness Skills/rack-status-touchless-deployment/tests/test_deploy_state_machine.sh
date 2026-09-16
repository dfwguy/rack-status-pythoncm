#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
src="$tmp/source/release"; mkdir -p "$src/bin/scripts" "$src/backend/tests" "$src/frontend" "$src/deploy"
printf 'instructions\n' >"$src/AGENTS.md"; printf 'services: {rack-status: {}}\n' >"$src/docker-compose.yml"
printf 'RACK_SCHEDULER=pythoncm\n' >"$src/rack-config.env"; printf 'RACK_WEB_AUTH_TOKEN_FILE = ""\n' >"$src/backend/app.py"
printf '# empty\n' >"$src/backend/requirements.txt"; printf '{}\n' >"$src/frontend/package.json"
printf '#!/bin/sh\nexit 0\n' >"$src/bin/prod-drift.sh"; chmod 755 "$src/bin/prod-drift.sh"
printf '#!/bin/sh\nexit 0\n' >"$src/bin/verify-deploy.sh"; chmod 755 "$src/bin/verify-deploy.sh"
printf '#!/bin/sh\nroot=$(cd "$(dirname "$0")/.." && pwd)\nprintf "{}\\n" > "$root/backend/build-info.json"\n' >"$src/bin/write-build-info.sh"; chmod 755 "$src/bin/write-build-info.sh"
printf '#!/bin/sh\nexit 0\n' >"$src/bin/scripts/check.sh"; chmod 755 "$src/bin/scripts/check.sh"
tar -C "$tmp/source" -czf "$tmp/release.tar.gz" release
sha=$(sha256sum "$tmp/release.tar.gz" | awk '{print $1}'); script_sha=$(sha256sum "$src/bin/scripts/check.sh" | awk '{print $1}')
mkdir "$tmp/mockbin" "$tmp/bcm" "$tmp/images" "$tmp/evidence"; printf key >"$tmp/key"; printf host >"$tmp/known"; printf token >"$tmp/token"; chmod 600 "$tmp/key" "$tmp/known" "$tmp/token"
chmod 700 "$tmp/bcm"
cat >"$tmp/mockbin/docker" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == info ]]; then exit 0; fi
if [[ "$1" == compose ]]; then
  [[ "$2" == version ]] && exit 0
  if [[ " $* " == *' config '* ]]; then
    jq -cn --arg svc "$NVIS_COMPOSE_SERVICE" --arg ctr "$NVIS_CONTAINER" \
      --arg key "$RACK_SSH_KEY_PATH" --arg kh "$RACK_KNOWN_HOSTS_PATH" --arg bcm "$RACK_BCM_CREDENTIALS_PATH" --arg tok "$RACK_WEB_AUTH_TOKEN_FILE" --arg img "$RACK_VALIDATION_CONTAINER_ROOT" \
      '{services:{($svc):{container_name:$ctr,environment:{RACK_WEB_AUTH_TOKEN:"",RACK_WEB_AUTH_TOKEN_FILE:"/run/secrets/web-auth-token"},volumes:[$key,$kh,$bcm,$tok,$img]|map({type:"bind",source:.,target:(if .==$tok then "/run/secrets/web-auth-token" else . end),read_only:true})}}}'
    exit 0
  fi
  [[ " $* " == *' up '* ]] && { touch "$MOCK_DOCKER_STATE"; exit 0; }
  [[ " $* " == *' down '* ]] && { rm -f "$MOCK_DOCKER_STATE"; exit 0; }
  [[ " $* " == *' build '* ]] && exit 0
fi
if [[ "$1" == inspect ]]; then
  [[ -f "$MOCK_DOCKER_STATE" ]] || exit 1
  case "$*" in *Config.Image*) echo "rack-status:${NVIS_RELEASE_SHA256:0:16}";; *"{{.Image}}"*) echo image-id;; *"{{.Id}}"*) echo container-id;; *) exit 0;; esac
  exit 0
fi
if [[ "$1" == image && "$2" == inspect ]]; then echo image-id; exit 0; fi
if [[ "$1" == exec ]]; then echo release.test-build; exit 0; fi
exit 0
MOCK
cat >"$tmp/mockbin/ssh" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
cmd=${*: -1}
if [[ "$cmd" == *'test -d'* ]]; then exit 1; fi
if [[ "$cmd" == *'sha256sum'* ]]; then printf '%s  remote\n' "$MOCK_SCRIPT_SHA"; exit 0; fi
if [[ "$cmd" == *'.scripts-stage-'* ]]; then cat >/dev/null; exit 0; fi
exit 0
MOCK
cat >"$tmp/mockbin/curl" <<'MOCK'
#!/usr/bin/env bash
printf '{}\n'
MOCK
cat >"$tmp/mockbin/npm" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK
cat >"$tmp/mockbin/python3" <<MOCK
#!/usr/bin/env bash
set -e
if [[ "\${1:-}" == -m && "\${2:-}" == venv ]]; then
  mkdir -p "\$3/bin"
  printf '#!/bin/sh\\nexit 0\\n' >"\$3/bin/pip"; chmod 755 "\$3/bin/pip"
  printf '#!/bin/sh\\nexit 0\\n' >"\$3/bin/python"; chmod 755 "\$3/bin/python"
  exit 0
fi
exec /usr/bin/python3 "\$@"
MOCK
chmod 755 "$tmp/mockbin"/*
cat >"$tmp/deploy.env" <<EOF
NVIS_RELEASE_TARBALL=$tmp/release.tar.gz
NVIS_RELEASE_SHA256=$sha
NVIS_DEPLOY_PATH=$tmp/deploy-root
NVIS_DEPLOY_HOST=$(hostname)
NVIS_COMPOSE_PROJECT=nvis-test
NVIS_COMPOSE_SERVICE=rack-status
NVIS_CONTAINER=nvis-test-container
NVIS_VERIFY_URL=http://127.0.0.1:59999
NVIS_SITE_PROFILE=nscale-tx
NVIS_EVIDENCE_ROOT=$tmp/evidence
RACK_SSH_HOST=head.invalid
RACK_SSH_USER=cmods
RACK_SSH_KEY_PATH=$tmp/key
RACK_KNOWN_HOSTS_PATH=$tmp/known
RACK_REMOTE_BASE_PATH=/home/cmods/nvis-rack-status
RACK_WEB_AUTH_TOKEN_FILE=$tmp/token
RACK_BCM_CREDENTIALS_PATH=$tmp/bcm
RACK_SCHEDULER=pythoncm
RACK_VALIDATION_CONTAINER_ROOT=$tmp/images
NVIS_ALLOW_INITIAL_SCRIPT_INSTALL=YES
NVIS_SMOKE_APPROVED=NO
EOF
chmod 600 "$tmp/deploy.env"
export MOCK_DOCKER_STATE="$tmp/docker.running" MOCK_SCRIPT_SHA="$script_sha"
PATH="$tmp/mockbin:$PATH" NVIS_DEPLOY_ENV="$tmp/deploy.env" "$ROOT/scripts/preflight.sh" >/dev/null
out=$(PATH="$tmp/mockbin:$PATH" NVIS_DEPLOY_ENV="$tmp/deploy.env" "$ROOT/scripts/deploy.sh" 2>&1) || { printf '%s\n' "$out" >&2; find "$tmp/evidence" -type f -maxdepth 4 -exec sh -c 'echo FILE:$1; cat "$1"' _ {} \; >&2; exit 1; }
grep -q '^DEPLOYED ' <<<"$out"
out=$(PATH="$tmp/mockbin:$PATH" NVIS_DEPLOY_ENV="$tmp/deploy.env" "$ROOT/scripts/deploy.sh" 2>&1) || { printf '%s\n' "$out" >&2; exit 1; }
grep -q '^DEPLOYED ' <<<"$out"
report="$tmp/evidence/state/$sha/deployment-report.txt"
grep -q 'status=DEPLOYED' "$report"; grep -q 'gate=preflight result=SKIPPED' "$report"
out=$(PATH="$tmp/mockbin:$PATH" NVIS_DEPLOY_ENV="$tmp/deploy.env" "$ROOT/scripts/rollback.sh" --state-dir "$tmp/evidence/state/$sha")
grep -q '^ROLLED_BACK ' <<<"$out"; [[ ! -e "$MOCK_DOCKER_STATE" ]]
grep -q 'status=ROLLED_BACK' "$tmp/evidence/state/$sha/rollback-report.txt"
printf 'PASS hermetic checkpointed deployment and rollback state machine\n'
