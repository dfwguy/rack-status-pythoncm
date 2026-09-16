#!/usr/bin/env bash
# Idempotent, checkpointed Rack Status release deployment. Run on the app server.
set -Eeuo pipefail
umask 077
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/lib.sh"
NVIS_PHASE=bootstrap
nvis_load_env

release_sha=${NVIS_RELEASE_SHA256,,}
state_root=${NVIS_STATE_ROOT:-$NVIS_EVIDENCE_ROOT/state}
run_dir="$state_root/$release_sha"
checkpoint_dir="$run_dir/checkpoints"
NVIS_REPORT="$run_dir/deployment-report.txt"; export NVIS_REPORT
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

staging="$run_dir/extracted"
release_root_file="$run_dir/release-root"
release_dir="$NVIS_DEPLOY_PATH/releases/$release_sha"
compose_env="$run_dir/compose.env"
compose_override="$run_dir/docker-compose.release.yml"
rollback_state="$run_dir/rollback.env"
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

do_extract() {
  rm_target="$staging"
  [[ ! -e "$rm_target" ]] || nvis_die "uncheckpointed extraction directory already exists: $rm_target"
  root=$(python3 "$SCRIPT_DIR/safe_extract.py" "$NVIS_RELEASE_TARBALL" "$staging" \
    --max-files "${NVIS_ARCHIVE_MAX_FILES:-50000}" --max-bytes "${NVIS_ARCHIVE_MAX_BYTES:-8589934592}")
  for f in AGENTS.md docker-compose.yml bin/prod-drift.sh bin/verify-deploy.sh bin/write-build-info.sh; do
    [[ -e "$root/$f" ]] || nvis_die "release schema missing $f"
  done
  printf '%s\n' "$root" >"$release_root_file"
}

do_tests() {
  local root; root=$(<"$release_root_file")
  python3 "$SCRIPT_DIR/secret_scan.py" "$root"
  if [[ -d "$root/.git" ]]; then git -C "$root" diff --check; fi
  find "$root/bin" "$root/deploy" -type f \( -name '*.sh' -o -name '*.sbatch' \) -print0 2>/dev/null | xargs -0 -r -n1 bash -n
  py=${NVIS_PYTHON_BIN:-python3}
  "$py" -m venv "$run_dir/test-venv"
  "$run_dir/test-venv/bin/pip" install --disable-pip-version-check -r "$root/backend/requirements.txt"
  (cd "$root" && "$run_dir/test-venv/bin/python" -m pytest -q backend/tests)
  npm_bin=${NVIS_NPM_BIN:-npm}
  (cd "$root/frontend" && "$npm_bin" ci && "$npm_bin" test && "$npm_bin" run build)
  rm -rf -- "$root/frontend/node_modules"
}

remote_manifest() {
  local remote_dir=$1
  nvis_ssh_opts
  ssh "${NVIS_SSH_OPTS[@]}" "$RACK_SSH_USER@$RACK_SSH_HOST" \
    "cd '$remote_dir' 2>/dev/null && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum" </dev/null
}

do_drift() {
  local current='' current_scripts=''
  [[ -L "$NVIS_DEPLOY_PATH/current" ]] && current=$(readlink -f "$NVIS_DEPLOY_PATH/current")
  if [[ -z "$current" && -e "$NVIS_DEPLOY_PATH/.git" ]]; then current=$NVIS_DEPLOY_PATH; fi
  if docker inspect "$NVIS_CONTAINER" >/dev/null 2>&1; then
    [[ -n "$current" && -L "$NVIS_DEPLOY_PATH/current" ]] || nvis_die 'running container is not a previously managed release; perform a reviewed adoption first'
    if [[ -d "$current/.git" ]]; then
      git -C "$current" diff --quiet && git -C "$current" diff --cached --quiet || nvis_die 'live application tree has tracked drift'
      [[ -z $(git -C "$current" ls-files --others --exclude-standard) ]] || nvis_die 'live application tree has untracked drift'
    else
      [[ -f "$current/.nvis-release-manifest" ]] || nvis_die 'current release has no immutable manifest'
      (cd "$current" && sha256sum -c .nvis-release-manifest >/dev/null) || nvis_die 'live application tree differs from its manifest'
    fi
    current_scripts="$current/bin/scripts"
  fi
  remote_dir="$RACK_REMOTE_BASE_PATH/bin/scripts"
  if nvis_ssh_opts && ssh "${NVIS_SSH_OPTS[@]}" "$RACK_SSH_USER@$RACK_SSH_HOST" "test -d '$remote_dir'" </dev/null; then
    [[ -n "$current_scripts" ]] || nvis_die 'remote scripts exist but no local baseline is attributable'
    local expected actual
    expected=$(mktemp); actual=$(mktemp)
    nvis_tree_manifest "$current_scripts" >"$expected"
    remote_manifest "$remote_dir" >"$actual"
    python3 - "$expected" "$actual" <<'PY' || { rm -f "$expected" "$actual"; nvis_die 'head-node canonical script tree has drift'; }
import sys
def read(path):
    out={}
    for line in open(path):
        sha, name=line.rstrip('\n').split(None,1); out[name.strip()]=sha
    return out
expected, actual=read(sys.argv[1]), read(sys.argv[2])
bad=[p for p,s in expected.items() if actual.get(p)!=s]
if bad:
    print('missing/mismatched canonical scripts: '+','.join(bad[:20]), file=sys.stderr)
    raise SystemExit(1)
PY
    rm -f "$expected" "$actual"
  else
    [[ "${NVIS_ALLOW_INITIAL_SCRIPT_INSTALL:-NO}" == YES ]] || nvis_die 'remote script tree is absent; set sealed NVIS_ALLOW_INITIAL_SCRIPT_INSTALL=YES for first install'
  fi
}

do_snapshot() {
  local old_image='' old_id='' old_release='' old_sha='' old_state='' remote_existed=NO
  if docker inspect "$NVIS_CONTAINER" >/dev/null 2>&1; then
    old_image=$(docker inspect -f '{{.Config.Image}}' "$NVIS_CONTAINER")
    old_id=$(docker inspect -f '{{.Id}}' "$NVIS_CONTAINER")
    [[ -L "$NVIS_DEPLOY_PATH/current" ]] && old_release=$(readlink -f "$NVIS_DEPLOY_PATH/current")
    [[ -f "$old_release/.nvis-release-sha256" ]] || nvis_die 'managed current release lacks release identity'
    old_sha=$(<"$old_release/.nvis-release-sha256"); old_state="$state_root/$old_sha"
    [[ -f "$old_state/compose.env" && -f "$old_state/docker-compose.release.yml" ]] || nvis_die 'managed current release lacks rollback Compose evidence'
  fi
  nvis_ssh_opts
  if ssh "${NVIS_SSH_OPTS[@]}" "$RACK_SSH_USER@$RACK_SSH_HOST" "test -d '$RACK_REMOTE_BASE_PATH/bin/scripts'" </dev/null; then
    remote_existed=YES
    remote_manifest "$RACK_REMOTE_BASE_PATH/bin/scripts" >"$run_dir/remote-scripts-before.manifest"
    ssh "${NVIS_SSH_OPTS[@]}" "$RACK_SSH_USER@$RACK_SSH_HOST" \
      "cd '$RACK_REMOTE_BASE_PATH' && tar -cf - bin/scripts" >"$run_dir/remote-scripts-before.tar"
    sha256sum "$run_dir/remote-scripts-before.tar" >"$run_dir/remote-scripts-before.tar.sha256"
  fi
  printf 'NVIS_OLD_IMAGE=%q\nNVIS_OLD_CONTAINER_ID=%q\nNVIS_OLD_RELEASE=%q\nNVIS_OLD_STATE=%q\nNVIS_NEW_RELEASE=%q\nNVIS_NEW_STATE=%q\nNVIS_NEW_CONTAINER=%q\nNVIS_REMOTE_SCRIPTS_EXISTED=%q\n' \
    "$old_image" "$old_id" "$old_release" "$old_state" "$release_dir" "$run_dir" "$NVIS_CONTAINER" "$remote_existed" >"$rollback_state"
  chmod 600 "$rollback_state"
  nvis_record "rollback_container_id=${old_id:-none} rollback_image=${old_image:-none} rollback_scripts=$run_dir/remote-scripts-before.tar"
}

do_install() {
  local root; root=$(<"$release_root_file")
  [[ ! -e "$release_dir" ]] || nvis_die "release destination already exists without checkpoint: $release_dir"
  mkdir -p "$NVIS_DEPLOY_PATH/releases"
  mv "$root" "$release_dir"
  printf '%s\n' "$release_sha" >"$release_dir/.nvis-release-sha256"
  printf '%s\n' "$release_dir" >"$release_root_file"
}

do_config() {
  [[ -n $(rg -l 'RACK_WEB_AUTH_TOKEN_FILE' "$release_dir/backend" 2>/dev/null) ]] || nvis_die 'release lacks web-token-file support'
  cat >"$compose_env" <<EOF
RACK_SSH_HOST=$RACK_SSH_HOST
RACK_SSH_KEY_PATH=$RACK_SSH_KEY_PATH
RACK_KNOWN_HOSTS_PATH=$RACK_KNOWN_HOSTS_PATH
RACK_BCM_CREDENTIALS_PATH=$RACK_BCM_CREDENTIALS_PATH
RACK_WEB_AUTH_TOKEN=
EOF
  chmod 600 "$compose_env"
  cat >"$compose_override" <<EOF
services:
  $NVIS_COMPOSE_SERVICE:
    container_name: $NVIS_CONTAINER
    image: rack-status:${release_sha:0:16}
    environment:
      RACK_WEB_AUTH_TOKEN: ""
      RACK_WEB_AUTH_TOKEN_FILE: /run/secrets/web-auth-token
      RACK_SCHEDULER: pythoncm
      RACK_SSH_HOST: $RACK_SSH_HOST
      RACK_SSH_USER: $RACK_SSH_USER
      RACK_REMOTE_BASE_PATH: $RACK_REMOTE_BASE_PATH
    volumes:
      - $RACK_WEB_AUTH_TOKEN_FILE:/run/secrets/web-auth-token:ro
      - $RACK_VALIDATION_CONTAINER_ROOT:$RACK_VALIDATION_CONTAINER_ROOT:ro
EOF
  [[ -z "${NVIS_COMPOSE_OVERRIDE:-}" ]] || { [[ -f "$NVIS_COMPOSE_OVERRIDE" ]] || nvis_die 'site Compose override absent'; }
  if [[ -n "${NVIS_COMPOSE_OVERRIDE:-}" ]]; then
    printf '%s\n' "$NVIS_COMPOSE_OVERRIDE" >"$run_dir/site-override.path"
    sha256sum "$NVIS_COMPOSE_OVERRIDE" >"$run_dir/site-override.sha256"
  fi
  args=(-f "$release_dir/docker-compose.yml" -f "$compose_override")
  [[ -n "${NVIS_COMPOSE_OVERRIDE:-}" ]] && args+=(-f "$NVIS_COMPOSE_OVERRIDE")
  raw_config="$run_dir/.compose.raw.json"
  docker compose --project-name "$NVIS_COMPOSE_PROJECT" --project-directory "$release_dir" --env-file "$compose_env" "${args[@]}" config --format json >"$raw_config"
  chmod 600 "$raw_config"; printf '%s  compose-render.json\n' "$(sha256sum "$raw_config" | awk '{print $1}')" >"$run_dir/compose.render.sha256"
  python3 - "$raw_config" "$NVIS_COMPOSE_SERVICE" "$NVIS_CONTAINER" "$RACK_SSH_KEY_PATH" "$RACK_KNOWN_HOSTS_PATH" "$RACK_BCM_CREDENTIALS_PATH" "$RACK_WEB_AUTH_TOKEN_FILE" "$RACK_VALIDATION_CONTAINER_ROOT" "$run_dir/compose.redacted.json" <<'PY'
import json, pathlib, re, sys
c=json.load(open(sys.argv[1])); s=c.get('services',{}).get(sys.argv[2])
if not s or s.get('container_name') != sys.argv[3]: raise SystemExit('service/container contract mismatch')
mounts=s.get('volumes',[])
for m in mounts:
    if isinstance(m,dict) and m.get('type')=='bind':
        src=m.get('source','')
        if not pathlib.Path(src).exists(): raise SystemExit(f'bind source absent: {src}')
if not any(isinstance(m,dict) and m.get('target')=='/run/secrets/web-auth-token' and m.get('read_only') for m in mounts):
    raise SystemExit('web token is not mounted read-only')
sources={m.get('source') for m in mounts if isinstance(m,dict) and m.get('type')=='bind'}
missing=set(sys.argv[4:9])-sources
if missing: raise SystemExit('required bind source missing from Compose rendering: '+','.join(sorted(missing)))
env=s.get('environment',{})
if env.get('RACK_WEB_AUTH_TOKEN') not in ('',None): raise SystemExit('web token leaked into Compose environment')
if env.get('RACK_WEB_AUTH_TOKEN_FILE') != '/run/secrets/web-auth-token': raise SystemExit('web token file contract missing')
for service in c.get('services',{}).values():
    values=service.get('environment',{})
    for key in list(values):
        if re.search(r'(TOKEN|PASSWORD|SECRET|CREDENTIAL)', key, re.I): values[key]='<redacted>'
with open(sys.argv[9], 'w') as out: json.dump(c,out,indent=2,sort_keys=True); out.write('\n')
PY
  rm -f "$raw_config"
}

do_build() {
  "$release_dir/bin/write-build-info.sh" --stamp "release.${release_sha:0:16}"
  args=(-f "$release_dir/docker-compose.yml" -f "$compose_override")
  [[ -n "${NVIS_COMPOSE_OVERRIDE:-}" ]] && args+=(-f "$NVIS_COMPOSE_OVERRIDE")
  docker compose --project-name "$NVIS_COMPOSE_PROJECT" --project-directory "$release_dir" --env-file "$compose_env" "${args[@]}" build --pull "$NVIS_COMPOSE_SERVICE"
  docker image inspect "rack-status:${release_sha:0:16}" --format '{{.Id}}' >"$run_dir/new-image-id"
  nvis_tree_manifest "$release_dir" >"$release_dir/.nvis-release-manifest"
}

do_sync_scripts() {
  mutation_started=1
  local manifest="$run_dir/scripts.manifest" archive="$run_dir/scripts.tar"
  nvis_tree_manifest "$release_dir/bin/scripts" >"$manifest"
  tar -C "$release_dir/bin/scripts" -cf "$archive" .
  nvis_ssh_opts
  tar -C "$release_dir/bin/scripts" -cf - . | ssh "${NVIS_SSH_OPTS[@]}" "$RACK_SSH_USER@$RACK_SSH_HOST" \
    "set -eu; base='$RACK_REMOTE_BASE_PATH'; tmp=\"\$base/.scripts-stage-$release_sha\"; test ! -e \"\$tmp\"; mkdir -p \"\$tmp\" \"\$base/bin/scripts\"; tar -C \"\$tmp\" -xf -; cp -a \"\$tmp/.\" \"\$base/bin/scripts/\"; rm -r \"\$tmp\""
  while IFS= read -r line; do
    sha=${line%% *}; rel=${line#*  }; [[ "$rel" =~ ^\./[A-Za-z0-9._/-]+$ ]] || nvis_die 'unsafe script manifest path'
    got=$(ssh "${NVIS_SSH_OPTS[@]}" "$RACK_SSH_USER@$RACK_SSH_HOST" "sha256sum '$RACK_REMOTE_BASE_PATH/bin/scripts/${rel#./}'" | awk '{print $1}')
    [[ "$got" == "$sha" ]] || nvis_die "remote script checksum mismatch: $rel"
  done <"$manifest"
}

do_deploy() {
  mutation_started=1
  args=(-f "$release_dir/docker-compose.yml" -f "$compose_override")
  [[ -n "${NVIS_COMPOSE_OVERRIDE:-}" ]] && args+=(-f "$NVIS_COMPOSE_OVERRIDE")
  docker compose --project-name "$NVIS_COMPOSE_PROJECT" --project-directory "$release_dir" --env-file "$compose_env" "${args[@]}" up -d --no-build --force-recreate "$NVIS_COMPOSE_SERVICE"
}

do_verify() { NVIS_RELEASE_ROOT="$release_dir" "$SCRIPT_DIR/verify.sh" --release-root "$release_dir"; }
do_activate() {
  ln -s "$release_dir" "$NVIS_DEPLOY_PATH/.current.$release_sha"
  mv -Tf "$NVIS_DEPLOY_PATH/.current.$release_sha" "$NVIS_DEPLOY_PATH/current"
  nvis_record "release_sha256=$release_sha active_release=$release_dir image_id=$(<"$run_dir/new-image-id") config_sha256=$(awk '{print $1}' "$run_dir/compose.render.sha256")"
}
do_optional_smoke() {
  if [[ "$NVIS_SMOKE_APPROVED" == YES ]]; then "$SCRIPT_DIR/launch-smoke.sh"; else printf 'smoke=NOT_AUTHORIZED\n'; fi
}

run_phase preflight do_preflight
run_phase extract do_extract
run_phase tests do_tests
run_phase drift do_drift
run_phase snapshot do_snapshot
run_phase install do_install
run_phase config do_config
run_phase build do_build
run_phase scripts do_sync_scripts
run_phase deploy do_deploy
mutation_started=1
run_phase verify do_verify
run_phase activate do_activate
run_phase smoke do_optional_smoke
mutation_started=0
nvis_record 'status=DEPLOYED'
printf 'DEPLOYED release=%s container=%s report=%s\n' "$release_sha" "$NVIS_CONTAINER" "$NVIS_REPORT"
