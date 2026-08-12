#!/usr/bin/env bash
# deploy-app.sh — push this tree's built SPA and backend to a container deployment, then verify.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Why this exists as a script and not a runbook step: every OVA deploy so far has been a
# hand-rolled rsync, and the same failure has now happened twice -- the dashboard serving a
# black screen or 404s because index.html and the hashed assets disagreed. The knowledge that
# prevents it is one flag, in one place, and a flag nobody can remember at 6pm belongs in a file.
#
# THE FLAG: index.html must be synced with --inplace.
#
#   Vite renames every asset on each build (index-<hash>.js), so index.html and assets/ must move
#   together or the page requests a bundle that is not there. Compose bind-mounts index.html as a
#   SINGLE FILE, which binds the container to that file's *inode*. A default rsync writes a temp
#   file and renames it over the target, replacing the inode -- so the host shows the new file
#   while the container reads the old, now-unlinked one, forever. Only a restart re-resolves it.
#   With --inplace the inode survives, the container sees the change immediately, and no restart
#   is needed for a frontend-only deploy. (assets/ is a directory mount and is immune.)
#
# Backend .py files are also bind-mounted per file, so the same inode rule applies -- and Python
# is read at import, so those DO require a restart to take effect.
#
# Usage:
#   bin/deploy-app.sh <ssh-host> [deploy-path]
#   bin/deploy-app.sh --dry-run nscale-tx-ova
#
# Env:
#   NVIS_DEPLOY_PATH   deployment tree on the remote  (default /opt/observability/nvis-rack-status-v3)
#   NVIS_CONTAINER     container name                 (default nvis-rack-status-v3)
#   NVIS_SKIP_VERIFY=1 skip the post-deploy gate      (not recommended)
set -uo pipefail

DRY=""
if [ "${1:-}" = "--dry-run" ]; then DRY="--dry-run"; shift; fi

SSH_HOST="${1:-}"
DEPLOY_PATH="${2:-${NVIS_DEPLOY_PATH:-/opt/observability/nvis-rack-status-v3}}"
CONTAINER="${NVIS_CONTAINER:-nvis-rack-status-v3}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -z "$SSH_HOST" ]; then
  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 2
fi

say() { printf '  %s\n' "$*"; }
die() { printf '\033[31mdeploy-app: %s\033[0m\n' "$*" >&2; exit 1; }

printf 'deploy-app — %s:%s (container %s)%s\n' \
  "$SSH_HOST" "$DEPLOY_PATH" "$CONTAINER" "${DRY:+  [DRY RUN]}"

# A deploy from an unbuilt or half-built tree is the other way to serve a broken page.
[ -f "$HERE/static/spa/index.html" ] || die "static/spa/index.html is missing — run 'npm run build' in frontend/ first."
ref=$(grep -o 'index-[A-Za-z0-9_-]*\.js' "$HERE/static/spa/index.html" | head -1)
[ -n "$ref" ] || die "index.html references no bundle — the build did not finish."
[ -f "$HERE/static/spa/assets/$ref" ] || die "index.html wants assets/$ref, which is not in this tree. Rebuild."
say "local build is coherent (index.html -> $ref)"

# Assets first, so the bundle exists before any page can reference it. A directory mount, so a
# plain rsync is fine here; --delete keeps old hashes from accumulating forever.
rsync -az $DRY --delete "$HERE/static/spa/assets/" "$SSH_HOST:$DEPLOY_PATH/static/spa/assets/" \
  || die "asset sync failed"
say "assets synced"

# Then index.html, in place. See THE FLAG above -- this is the whole point of the script.
rsync -az $DRY --inplace --no-whole-file "$HERE/static/spa/index.html" \
  "$SSH_HOST:$DEPLOY_PATH/static/spa/index.html" || die "index.html sync failed"
say "index.html synced in place (inode preserved)"

# Backend + generated content. Only the files the deployment actually bind-mounts.
backend_changed=0
if [ -n "${NVIS_DEPLOY_BACKEND:-1}" ]; then
  out=$(rsync -az $DRY --inplace --no-whole-file --itemize-changes \
        "$HERE/backend/"*.py "$SSH_HOST:$DEPLOY_PATH/backend/" 2>&1) || die "backend sync failed"
  [ -n "$out" ] && backend_changed=1
  rsync -az $DRY --inplace --no-whole-file "$HERE/help_content.json" \
    "$SSH_HOST:$DEPLOY_PATH/help_content.json" || die "help_content sync failed"
  rsync -az $DRY --inplace --no-whole-file "$HERE/bin/verify-deploy.sh" \
    "$SSH_HOST:$DEPLOY_PATH/bin/verify-deploy.sh" || die "verify-deploy sync failed"
  say "backend modules synced$([ "$backend_changed" = 1 ] && echo ' (changed)' || echo ' (no change)')"
fi

if [ -n "$DRY" ]; then
  printf '\nDry run: nothing was written.\n'
  exit 0
fi

# Python is read at import, so a backend change needs a restart. A frontend-only deploy does not,
# because --inplace kept the inode -- which is exactly the restart this script is designed to avoid.
if [ "$backend_changed" = 1 ]; then
  say "restarting (backend changed)"
  ssh -o BatchMode=yes "$SSH_HOST" "docker restart $CONTAINER >/dev/null" || die "restart failed"
  sleep 18
else
  say "no restart needed (frontend-only deploy)"
fi

if [ "${NVIS_SKIP_VERIFY:-0}" = "1" ]; then
  printf '\nSkipped verification (NVIS_SKIP_VERIFY=1).\n'
  exit 0
fi

printf '\n'
ssh -o BatchMode=yes "$SSH_HOST" \
  "cd $DEPLOY_PATH && NVIS_CONTAINER=$CONTAINER bash bin/verify-deploy.sh"
