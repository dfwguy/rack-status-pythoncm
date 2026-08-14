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
#   bin/deploy-app.sh --force nscale-tx-ova    # proceed despite a changed deployment
#
# Env:
#   NVIS_DEPLOY_PATH   deployment tree on the remote  (default /opt/observability/nvis-rack-status-v3)
#   NVIS_CONTAINER     container name                 (default nvis-rack-status-v3)
#   NVIS_SKIP_VERIFY=1 skip the post-deploy gate      (not recommended)
set -uo pipefail

DRY=""
FORCE=""
while true; do
  case "${1:-}" in
    --dry-run) DRY="--dry-run"; shift ;;
    --force)   FORCE=1; shift ;;
    *) break ;;
  esac
done

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

# ---------------------------------------------------------------------------
# Has anyone changed the deployment since we last deployed to it?
#
# prod-drift.sh cannot answer this for the frontend: static/spa is gitignored, so a rebuilt bundle
# is invisible to a tool that diffs tracked files against a git ref. It also exits non-zero on
# untracked extras, of which this deployment permanently has thousands (a Node toolchain, .prebak
# files), so it cannot serve as a hard gate either.
#
# So we record what we shipped, and compare against it. On 2026-08-13 the deployment's own stale
# checkout was rebuilt in place at 12:50, reverting the frontend to a build predating the terminal
# work: the UI showed a deleted placeholder screen and a route fell through to the dashboard. Every
# consistency check passed, because host, container and served bundle all agreed with each other --
# consistently wrong. This is the check that notices.
MANIFEST=".nvis-deploy-manifest"
remote_state=$(ssh -o BatchMode=yes "$SSH_HOST" "
  cd $DEPLOY_PATH 2>/dev/null || exit 0
  printf 'live=%s\n' \"\$(grep -o 'index-[A-Za-z0-9_-]*\.js' static/spa/index.html 2>/dev/null | head -1)\"
  cat $MANIFEST 2>/dev/null
" 2>/dev/null)

live_ref=$(printf '%s' "$remote_state" | sed -n 's/^live=//p' | head -1)
last_ref=$(printf '%s' "$remote_state" | sed -n 's/^bundle=//p' | head -1)
last_when=$(printf '%s' "$remote_state" | sed -n 's/^deployed_at=//p' | head -1)

if [ -n "$last_ref" ] && [ -n "$live_ref" ] && [ "$last_ref" != "$live_ref" ]; then
  printf '\033[31mdeploy-app: the deployment changed since our last deploy.\033[0m\n' >&2
  printf '  we last shipped : %s  (%s)\n' "$last_ref" "${last_when:-unknown}" >&2
  printf '  now serving     : %s\n' "$live_ref" >&2
  printf '  Someone rebuilt or redeployed it. Deploying now overwrites their change without ever\n' >&2
  printf '  showing you what it was. Investigate first:\n' >&2
  printf '    ssh %s "cd %s && git log --oneline -3 && git status --short | head"\n' "$SSH_HOST" "$DEPLOY_PATH" >&2
  printf '  Re-run with --force once you know.\n' >&2
  [ -n "$FORCE" ] || exit 1
  say "forced past the manifest mismatch"
elif [ -n "$last_ref" ]; then
  say "deployment unchanged since our last deploy ($last_ref)"
else
  say "no deploy manifest yet - recording one after this deploy"
fi

# Advisory only: tracked-file drift. Reported, never gating, for the reason above.
if [ -x "$HERE/bin/prod-drift.sh" ] && [ -z "$DRY" ]; then
  changed=$(NVIS_DEPLOY_SSH="$SSH_HOST" NVIS_DEPLOY_PATH="$DEPLOY_PATH" \
            "$HERE/bin/prod-drift.sh" 2>/dev/null \
            | sed -n '/CHANGED on the deployment/,/present only on the deployment/p' \
            | grep -vE 'CHANGED on the deployment|present only on the deployment' || true)
  if [ -n "$changed" ]; then
    printf '\033[33m  tracked files edited ON the deployment (this deploy overwrites them):\033[0m\n'
    printf '%s\n' "$changed"
    printf '  Harvest them first if they matter: bin/prod-drift.sh --fetch <dir>\n'
  fi
fi

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

# Written only after a successful sync: a failed deploy must not claim to have shipped a bundle.
if ssh -o BatchMode=yes "$SSH_HOST" "cat > $DEPLOY_PATH/$MANIFEST" <<MANIFEST_EOF
bundle=$ref
commit=$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)
deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
deployed_by=${USER:-unknown}@$(hostname -s 2>/dev/null || echo unknown)
MANIFEST_EOF
then
  say "recorded deploy manifest ($ref)"
else
  # Not fatal — the deploy itself succeeded. But say so, because a missing manifest silently
  # disables the change-detection gate on the *next* deploy, which is the failure mode this whole
  # mechanism exists to prevent.
  printf '\033[33m  WARNING: could not write the deploy manifest; the next deploy cannot detect\n' >&2
  printf '           changes made to this deployment in the meantime.\033[0m\n' >&2
fi

if [ "${NVIS_SKIP_VERIFY:-0}" = "1" ]; then
  printf '\nSkipped verification (NVIS_SKIP_VERIFY=1).\n'
  exit 0
fi

printf '\n'
ssh -o BatchMode=yes "$SSH_HOST" \
  "cd $DEPLOY_PATH && NVIS_CONTAINER=$CONTAINER bash bin/verify-deploy.sh"
