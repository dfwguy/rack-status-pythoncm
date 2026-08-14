#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# verify-deploy.sh — prove a deployment actually works, not merely that it started.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Why this exists
#   "Container up and /login returns 200" passes with a completely broken product. On 2026-08-12
#   the dashboard served a black screen for about an hour while every one of those checks was
#   green: the SPA's index.html referenced a build whose chunks were no longer there, so all 164
#   asset requests 404'd. The page itself was fine. Everything inside it was missing.
#
#   Every check below is here because its absence caused a real outage:
#     * assets            — the black screen above.
#     * module imports    — a new backend module without a bind mount is an instant crash loop,
#                           because app.py is mounted and imports it.
#     * stale modules     — a bind mount that was declared but never activated (a `restart`
#                           instead of a `compose up -d`) silently runs the image's old copy.
#     * restarting state  — `docker ps` says "Up" for a container in a crash loop; it says
#                           "Restarting" only if you look.
#
# Usage
#   bin/verify-deploy.sh                       # local container (default port 5050)
#   NVIS_DEPLOY_SSH=host bin/verify-deploy.sh  # a remote deployment
#
# Env
#   NVIS_DEPLOY_SSH    ssh alias/user@host holding the deployment (omit for local)
#   NVIS_DEPLOY_PATH   deployment tree path       (default /opt/observability/nvis-rack-status-v3)
#   NVIS_CONTAINER     container name             (default nvis-rack-status-v3)
#   NVIS_VERIFY_PORT   port the app listens on    (default: read from the container's published port)
#   NVIS_VERIFY_PATH   page whose assets to check (default /login)
#
# Exit status is 0 only when every check passes, so it can gate a deploy.
set -uo pipefail

DEPLOY_SSH="${NVIS_DEPLOY_SSH:-}"
DEPLOY_PATH="${NVIS_DEPLOY_PATH:-/opt/observability/nvis-rack-status-v3}"
CONTAINER="${NVIS_CONTAINER:-nvis-rack-status-v3}"
PAGE="${NVIS_VERIFY_PATH:-/login}"

# Ask the container which port it publishes rather than guessing from where we are running.
# A guessed port makes the gate cry wolf: probing 5050 against a container serving 5056 reports
# "the SPA is not built" and a 404 on /login, which is indistinguishable from a real broken
# deploy and trains the reader to ignore the gate. Explicit override still wins.
PORT="${NVIS_VERIFY_PORT:-}"
if [ -z "$PORT" ]; then
  _inspect='docker inspect -f "{{range \$p, \$c := .NetworkSettings.Ports}}{{if \$c}}{{(index \$c 0).HostPort}} {{end}}{{end}}"'
  if [ -n "$DEPLOY_SSH" ]; then
    PORT=$(ssh -o BatchMode=yes "$DEPLOY_SSH" "$_inspect $CONTAINER" 2>/dev/null | awk '{print $1}')
  else
    PORT=$(eval "$_inspect $CONTAINER" 2>/dev/null | awk '{print $1}')
  fi
fi
if [ -z "$PORT" ]; then
  echo "verify-deploy: cannot determine the published port for '$CONTAINER'." >&2
  echo "  Is it running? Otherwise set NVIS_VERIFY_PORT explicitly." >&2
  exit 2
fi

# The body runs on whichever host owns the container, so the same checks serve local and remote.
read -r -d '' BODY <<'CHECKS'
set -uo pipefail
CONTAINER="__CONTAINER__"; PORT="__PORT__"; PAGE="__PAGE__"; T="__PATH__"
BASE="http://127.0.0.1:${PORT}"
fail=0
ok()   { printf "  \033[32mPASS\033[0m  %s\n" "$1"; }
bad()  { printf "  \033[31mFAIL\033[0m  %s\n" "$1"; fail=$((fail+1)); }
note() { printf "        %s\n" "$1"; }

# 1. The container is running, and not merely "Up" while looping.
state=$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo missing)
restarts=$(docker inspect -f '{{.RestartCount}}' "$CONTAINER" 2>/dev/null || echo 0)
if [ "$state" = "running" ]; then ok "container running (restarts: $restarts)"
else bad "container state is '$state'"; fi

# 2. Every backend module imports. app.py is bind-mounted, so an unmounted module it imports
#    takes the whole app down; this catches that before a user does.
if [ "$state" = "running" ]; then
  out=$(docker exec -w /app/backend "$CONTAINER" python3 -c "
import importlib, pathlib
bad=[]
for f in sorted(pathlib.Path('/app/backend').glob('*.py')):
    if f.stem in ('app','conftest'): continue
    try: importlib.import_module(f.stem)
    except Exception as e: bad.append(f'{f.stem} ({type(e).__name__})')
print(','.join(bad))" 2>/dev/null)
  [ -z "$out" ] && ok "every backend module imports" || { bad "modules failing to import"; note "$out"; }
fi

# 3. No module in the container is older than the tree. A declared-but-inactive bind mount
#    (from `docker restart` instead of `compose up -d`) shows up here and nowhere else.
if [ "$state" = "running" ] && [ -d "$T/backend" ]; then
  stale=""
  for f in $(docker exec "$CONTAINER" sh -c 'ls /app/backend/*.py' 2>/dev/null); do
    b=$(basename "$f"); [ -f "$T/backend/$b" ] || continue
    docker exec "$CONTAINER" cat "$f" 2>/dev/null | diff -q - "$T/backend/$b" >/dev/null 2>&1 || stale="$stale $b"
  done
  [ -z "$stale" ] && ok "no stale modules (container matches tree)" || { bad "stale modules — mount not active?"; note "$stale"; }
fi

# 3b. Every module in the tree exists in the container.
#
#     Check 3 compares only files the container already has, so a module the tree has and the
#     container does not is invisible to it -- it reports "container matches tree" while the code
#     is simply absent. This deployment mounts backend modules one file at a time, so a NEW module
#     is missing until its mount is added, and the image predates it. When the importer is a lazy
#     `import x` inside a try/except (the usual shape for an optional feature) the result is not a
#     crash but silence: report_store.py went missing on 2026-08-12 and reports just did not save.
if [ "$state" = "running" ] && [ -d "$T/backend" ]; then
  present=$(docker exec "$CONTAINER" sh -c 'ls /app/backend/*.py 2>/dev/null | xargs -n1 basename' 2>/dev/null)
  missing=""
  for f in "$T"/backend/*.py; do
    b=$(basename "$f")
    printf '%s\n' "$present" | grep -qx "$b" || missing="$missing $b"
  done
  if [ -z "$missing" ]; then
    ok "no missing modules (every tree module is in the container)"
  else
    bad "modules in the tree but NOT in the container — add a bind mount or rebuild the image"
    note "$missing"
  fi
fi

# 3c. Durable feature routes must still be registered.  A previous partial restore left the
#     frontend and history files in place while app.py silently lost these registrations, so the
#     Post-run Node Report rendered only "API error".  Treat that as a failed deployment.
if [ "$state" = "running" ]; then
  missing_routes=$(docker exec -w /app/backend "$CONTAINER" python3 -c '
import app
required = {
    "/api/deep-audits",
    "/api/deep-audits/<run_id>",
    "/api/node-issues",
    "/api/node-issues/sync-punchcard",
    "/api/post-run-node-report",
    "/api/post-run-node-report/mark",
}
actual = {str(rule) for rule in app.app.url_map.iter_rules()}
print(",".join(sorted(required - actual)))
' 2>/dev/null)
  if [ -z "$missing_routes" ]; then
    ok "post-run, node-issue, and deep-audit routes registered"
  else
    bad "durable feature routes missing from app.py"
    note "$missing_routes"
  fi
fi

# 3d. The production override bind-mounts host SPA assets over the files baked into the image.
#     Merely proving that the mounted index and mounted assets agree with each other can therefore
#     bless a completely stale UI. Require every file baked into the deployed image (including
#     lazy route chunks) to exist byte-for-byte in the mounted tree; unrelated older hashed chunks
#     are harmless and intentionally ignored.
if [ "$state" = "running" ]; then
  image=$(docker inspect -f '{{.Image}}' "$CONTAINER" 2>/dev/null)
  manifest_cmd='cd /app/static/spa && { sha256sum index.html; find assets -maxdepth 1 -type f -exec sha256sum {} \;; } | sort'
  image_manifest=$(docker run --rm --entrypoint sh "$image" -c "$manifest_cmd" 2>/dev/null)
  live_manifest=$(docker exec "$CONTAINER" sh -c "$manifest_cmd" 2>/dev/null)
  stale_spa=$(comm -23 \
    <(printf '%s\n' "$image_manifest") \
    <(printf '%s\n' "$live_manifest"))
  if [ -n "$image_manifest" ] && [ -z "$stale_spa" ]; then
    ok "mounted SPA contains every file baked into the deployed image"
  else
    bad "mounted SPA is stale or incomplete relative to the deployed image"
    [ -n "$stale_spa" ] && note "$(printf '%s\n' "$stale_spa" | head -3)"
  fi
fi

# 4. THE check. Fetch the page and confirm every asset it references resolves. A hashed bundle
#    that has moved on leaves the HTML perfectly serveable and the product blank.
html=$(curl -s --max-time 15 "${BASE}${PAGE}")
assets=$(printf '%s' "$html" | grep -oE 'assets/[A-Za-z0-9_.-]+\.(js|css)' | sed 's|assets/||' | sort -u)
if [ -z "$assets" ]; then
  bad "page ${PAGE} references no assets — is the SPA built?"
else
  miss=0; total=0
  for a in $assets; do
    total=$((total+1))
    code=$(curl -s -o /dev/null --max-time 10 -w "%{http_code}" "${BASE}/assets/$a")
    [ "$code" = "200" ] || { miss=$((miss+1)); [ "$miss" -le 3 ] && note "$code /assets/$a"; }
  done
  [ "$miss" -eq 0 ] && ok "SPA assets resolve ($total checked)" || bad "$miss of $total SPA assets missing"
fi

# 5. The app answers.
code=$(curl -s -o /dev/null --max-time 10 -w "%{http_code}" "${BASE}${PAGE}")
[ "$code" = "200" ] || [ "$code" = "302" ] && ok "GET ${PAGE} -> $code" || bad "GET ${PAGE} -> $code"

# 6. Nothing exploded on the way up.
errs=$(docker logs --since 5m "$CONTAINER" 2>&1 | grep -icE "ModuleNotFoundError|Worker failed to boot|ImportError" || true)
[ "$errs" -eq 0 ] && ok "no import errors in recent logs" || { bad "$errs import/boot errors in the last 5m"; note "$(docker logs --since 5m "$CONTAINER" 2>&1 | grep -iE 'ModuleNotFoundError|Worker failed to boot' | tail -2)"; }

echo
if [ "$fail" -eq 0 ]; then echo "deployment verified."; else echo "VERIFICATION FAILED: $fail check(s)."; fi
exit "$fail"
CHECKS

BODY="${BODY//__CONTAINER__/$CONTAINER}"
BODY="${BODY//__PORT__/$PORT}"
BODY="${BODY//__PAGE__/$PAGE}"
BODY="${BODY//__PATH__/$DEPLOY_PATH}"

if [ -n "$DEPLOY_SSH" ]; then
  echo "verify-deploy — $DEPLOY_SSH:$CONTAINER on port $PORT"
  printf '%s' "$BODY" | ssh "$DEPLOY_SSH" 'cat > /tmp/nvis-verify.sh && bash /tmp/nvis-verify.sh'
else
  echo "verify-deploy — local $CONTAINER on port $PORT"
  printf '%s' "$BODY" | bash
fi
