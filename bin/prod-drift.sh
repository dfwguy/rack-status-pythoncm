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
# prod-drift.sh — find files a live deployment changed that git does not have.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Why this exists
#   On a live cluster people fix things in place, and those edits land where git never sees
#   them:
#     * the deployment tree bind-mounted into the container (backend/*.py), and
#     * the head node's bin/scripts copy, which is what the connection actually executes
#       whenever the target endpoint is remote.
#   The next deploy overwrites both. Run this BEFORE deploying so real fixes get harvested
#   instead of destroyed.
#
# What it compares against (this is the important part)
#   By default the ref is the DEPLOYMENT'S OWN base commit, read from the tree's git HEAD —
#   not your working HEAD. That is what separates the two cases people conflate:
#     * differs from the deployment's base  -> someone edited prod. Harvest it.
#     * differs from your HEAD only         -> you are simply ahead. The deploy delivers it.
#   Comparison is by content (sha256), never mtime: a deploy copy rewrites mtimes on files
#   whose content never changed, so timestamps report the whole tree as drifted.
#
# Usage
#   bin/prod-drift.sh [--ref REF] [--surface tree|scripts|all] [--fetch DIR]
#
#   --ref REF      compare against this ref instead of the deployment's base commit
#   --surface      which surface to check (default: all)
#   --fetch DIR    copy each changed remote file to DIR/<surface>/remote/<path> and the git
#                  version to DIR/<surface>/gitref/<path>, and print the 3-way merge command.
#                  Files that exist ONLY on the deployment are pulled too, to
#                  DIR/<surface>/prodonly/<path> — they have no git copy to merge against, so
#                  losing one loses it outright. Capped at NVIS_DRIFT_FETCH_CAP (default 40)
#                  files, since each fetch is an SSH round trip.
#
# Env
#   NVIS_DEPLOY_SSH        ssh alias / user@host holding the deployment tree
#   NVIS_DEPLOY_PATH       path of that deployment tree
#   RACK_SSH_HOST          head node holding bin/scripts (same names the app uses)
#   RACK_SSH_USER          default: root
#   RACK_SSH_KEY           optional private key
#   RACK_SSH_PORT          optional port
#   RACK_REMOTE_BASE_PATH  remote install root (bin/scripts lives under it)
#   NVIS_HEAD_VIA          optional: reach the head node by hopping through this ssh alias
#                          (Nscale TX: the OVA is the only route to the head node)
#   NVIS_DRIFT_FETCH_CAP   max remote-only files --fetch will pull (default 40)
#
# Example — Nscale TX (head node only reachable via the OVA)
#   NVIS_DEPLOY_SSH=nscale-tx-ova \
#   NVIS_DEPLOY_PATH=/opt/observability/nvis-rack-status-v3 \
#   NVIS_HEAD_VIA=nscale-tx-ova RACK_SSH_HOST=192.168.10.2 RACK_SSH_USER=cmods \
#   RACK_SSH_KEY=/home/nvis/.ssh/id_bcm RACK_REMOTE_BASE_PATH=/home/cmods/nvis-rack-status \
#     bin/prod-drift.sh --fetch /tmp/drift
#
# Exit status: 0 = no drift, 1 = drift found, 2 = usage/connection error.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REF=""
SURFACE="all"
FETCH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref)     REF="${2:?--ref needs a git ref}"; shift 2 ;;
    --surface) SURFACE="${2:?--surface needs tree|scripts|all}"; shift 2 ;;
    --fetch)   FETCH="${2:?--fetch needs a directory}"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)         echo "prod-drift: unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$SURFACE" in tree|scripts|all) ;; *) echo "prod-drift: bad --surface" >&2; exit 2 ;; esac

# Run a shell snippet on a remote host. The snippet is base64-encoded because the head node is
# reached by nesting ssh inside ssh: an unencoded command gets re-parsed by the intermediate
# shell, so `cd x && find .` silently splits and the find runs on the JUMP host instead of the
# target. Base64 is alphanumeric, so it survives any number of hops intact.
remote_sh() {
  local where="$1" snippet="$2" b64
  b64="$(printf '%s' "$snippet" | base64 | tr -d '\n')"
  local inner="echo ${b64} | base64 -d | sh"
  case "$where" in
    deploy)
      ssh "${NVIS_DEPLOY_SSH}" "${inner}"
      ;;
    head)
      local h="ssh -o BatchMode=yes"
      [[ -n "${RACK_SSH_KEY:-}" ]] && h+=" -i ${RACK_SSH_KEY}"
      [[ -n "${RACK_SSH_PORT:-}" ]] && h+=" -p ${RACK_SSH_PORT}"
      h+=" ${RACK_SSH_USER:-root}@${RACK_SSH_HOST}"
      if [[ -n "${NVIS_HEAD_VIA:-}" ]]; then
        # Single-quote the inner ssh invocation so the jump host's shell hands it to ssh whole.
        ssh "${NVIS_HEAD_VIA}" "${h} '${inner}'"
      else
        $h "${inner}"
      fi
      ;;
  esac
}

# sha256 of every file under a remote directory, as "<sha>  <relpath>".
# Prunes dependency/venv/cache trees rather than filtering after the fact, so a deployment
# with a local venv does not take minutes to walk.
hash_snippet() {
  local dir="$1"
  cat <<SNIP
cd '${dir}' 2>/dev/null || exit 9
find . \\
  \\( -name .git -o -name node_modules -o -name __pycache__ -o -name .venv -o -name venv \\
     -o -name .ruff_cache -o -name .pytest_cache -o -name .mypy_cache -o -name var \\
     -o -name dist -o -name .cursor \\) -prune -o \\
  -type f ! -name '*.pyc' ! -name '*.bak' ! -name '*.bak.*' -print0 \\
  | xargs -0 -r sha256sum 2>/dev/null | sort -k2
SNIP
}

compare_surface() {
  local label="$1" where="$2" remote_dir="$3" prefix="$4" ref="$5"
  local tmp; tmp="$(mktemp)"
  if ! remote_sh "$where" "$(hash_snippet "$remote_dir")" > "$tmp" 2>/dev/null || [[ ! -s "$tmp" ]]; then
    echo "  !! could not list ${remote_dir} (check connectivity / path)"
    rm -f "$tmp"; return 2
  fi
  REPO_ROOT="$REPO_ROOT" REF="$ref" LABEL="$label" PREFIX="$prefix" \
  REMOTE_DIR="$remote_dir" FETCH="$FETCH" WHERE="$where" \
  NVIS_DEPLOY_SSH="${NVIS_DEPLOY_SSH:-}" NVIS_HEAD_VIA="${NVIS_HEAD_VIA:-}" \
  RACK_SSH_HOST="${RACK_SSH_HOST:-}" RACK_SSH_USER="${RACK_SSH_USER:-root}" \
  RACK_SSH_KEY="${RACK_SSH_KEY:-}" RACK_SSH_PORT="${RACK_SSH_PORT:-}" \
  python3 "$REPO_ROOT/bin/lib/prod_drift_compare.py" "$tmp"
  local rc=$?
  rm -f "$tmp"
  return $rc
}

# Resolve the ref: the deployment's own base commit unless told otherwise.
BASE_REF="$REF"
if [[ -z "$BASE_REF" ]]; then
  if [[ -n "${NVIS_DEPLOY_SSH:-}" && -n "${NVIS_DEPLOY_PATH:-}" ]]; then
    BASE_REF="$(remote_sh deploy "cd '${NVIS_DEPLOY_PATH}' && git rev-parse HEAD 2>/dev/null" \
                 2>/dev/null | tr -d '\r\n' || true)"
  fi
  [[ -n "$BASE_REF" ]] || BASE_REF="HEAD"
fi
git -C "$REPO_ROOT" rev-parse --verify -q "$BASE_REF" >/dev/null 2>&1 || {
  echo "prod-drift: ref '${BASE_REF}' is not in this repo — fetch it, or pass --ref" >&2; exit 2; }

echo "prod-drift — live deployment vs '${BASE_REF}'"
if [[ -z "$REF" ]]; then
  echo "  (the deployment's own base commit: $(git -C "$REPO_ROOT" log --oneline -1 "$BASE_REF"))"
  echo "  anything listed below was changed ON the deployment, so a deploy would overwrite it."
fi
echo

drift=0
if [[ "$SURFACE" == "tree" || "$SURFACE" == "all" ]]; then
  echo "== deployment tree =="
  if [[ -z "${NVIS_DEPLOY_SSH:-}" || -z "${NVIS_DEPLOY_PATH:-}" ]]; then
    echo "  (skipped: set NVIS_DEPLOY_SSH and NVIS_DEPLOY_PATH)"
  else
    echo "  ${NVIS_DEPLOY_SSH}:${NVIS_DEPLOY_PATH}"
    compare_surface "tree" deploy "${NVIS_DEPLOY_PATH}" "" "$BASE_REF" || drift=1
  fi
  echo
fi

if [[ "$SURFACE" == "scripts" || "$SURFACE" == "all" ]]; then
  echo "== head node bin/scripts (what the connection actually executes) =="
  if [[ -z "${RACK_SSH_HOST:-}" ]]; then
    echo "  (skipped: set RACK_SSH_HOST)"
  else
    base="${RACK_REMOTE_BASE_PATH:-/home/cmsupport/nvis-rack-status}"
    echo "  ${RACK_SSH_USER:-root}@${RACK_SSH_HOST}:${base}/bin/scripts"
    compare_surface "scripts" head "${base}/bin/scripts" "bin/scripts/" "$BASE_REF" || drift=1
  fi
  echo
fi

if [[ "$drift" -ne 0 ]]; then
  echo "Drift found — harvest it into git BEFORE deploying."
  [[ -n "$FETCH" ]] && echo "Remote and git copies are under ${FETCH}/."
  exit 1
fi
echo "No drift: the live deployment matches '${BASE_REF}'."
