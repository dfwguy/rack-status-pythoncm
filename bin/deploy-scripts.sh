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
# deploy-scripts.sh — sync the repo's bin/scripts/ to the cluster head node.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# The repo's bin/scripts/ is the SOURCE OF TRUTH; the head node (NFS) holds a deployed copy
# under RACK_REMOTE_BASE_PATH/scripts/multi-platform (or the legacy bin/scripts on a deployment
# that has not been migrated -- detected, not guessed). This wraps a checksum-based rsync so "sync scripts
# to the head" is ONE idempotent command instead of per-file scp (which silently drifts).
# Checksum (-c) pushes only real content changes; Python build artifacts are excluded; no
# --delete by default, so head-only files (e.g. customer additions) are preserved.
#
# Usage:
#   bin/deploy-scripts.sh [--dry-run] [--delete] [user@host | ssh-alias]
#
# Target resolution (first that applies):
#   1. explicit  user@host  or an  ssh-alias  argument
#   2. RACK_SSH_USER@RACK_SSH_HOST from the environment
#
# Env (same names the app uses in SSH mode — see .cursor/rules/local-dev.mdc):
#   RACK_SSH_HOST          head node hostname/IP (required unless a target arg is given)
#   RACK_SSH_USER          SSH user (default: root)
#   RACK_SSH_KEY           SSH private key path (optional; omit when using an ssh-alias)
#   RACK_SSH_PORT          SSH port (optional; default from ssh config / 22)
#   RACK_SSH_KNOWN_HOSTS   known_hosts file; when set, the head's host key is verified against it
#   RACK_REMOTE_BASE_PATH  remote install root (default: /home/cmsupport/nvis-rack-status)
#
# Examples:
#   bin/deploy-scripts.sh boston-slogin              # via an ~/.ssh/config alias
#   bin/deploy-scripts.sh --dry-run boston-slogin    # preview what would change
#   bin/deploy-scripts.sh --tree nscale-tx-node      # force the layered layout
#   bin/deploy-scripts.sh --legacy mbcm01            # force the flat bin/scripts
#   RACK_SSH_HOST=10.0.0.5 RACK_SSH_KEY=~/.ssh/id bin/deploy-scripts.sh
#
# Layout:
#   --tree    <base>/scripts/multi-platform/  plus bin/scripts/platform/<id>/ -> <base>/scripts/<id>/
#   --legacy  <base>/bin/scripts/
#   (default) whichever the head node already has
#
# Never touched by a deploy: <base>/scripts/custom/ (operator-added scripts) and
# <base>/scripts/project-*/ (per-project copies). This script updates what ships with the app.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${REPO_ROOT}/bin/scripts/"

DRY=""
DELETE=""
TARGET=""
# auto: the tree when the head node already has one, the flat directory otherwise. That way the
# first `bootstrap-project-scripts.sh` migrates a deployment and every deploy after it lands in
# the right place with no flag to remember -- and a site that has not migrated is untouched.
LAYOUT="auto"
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--dry-run) DRY="-n"; shift ;;
    --delete)     DELETE="--delete"; shift ;;
    --tree)       LAYOUT="tree"; shift ;;
    --legacy)     LAYOUT="legacy"; shift ;;
    -h|--help)    grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)           echo "deploy-scripts: unknown option: $1" >&2; exit 2 ;;
    *)            TARGET="$1"; shift ;;
  esac
done

if [[ -z "${TARGET}" ]]; then
  [[ -n "${RACK_SSH_HOST:-}" ]] || {
    echo "deploy-scripts: no target — pass user@host / an ssh-alias, or set RACK_SSH_HOST" >&2
    exit 2
  }
  TARGET="${RACK_SSH_USER:-root}@${RACK_SSH_HOST}"
fi

BASE="${RACK_REMOTE_BASE_PATH:-/home/cmsupport/nvis-rack-status}"
TREE="${NVIS_SCRIPTS_TREE:-${BASE%/}/scripts}"

# SSH transport for rsync. Key/port only when explicitly set (an ssh-alias supplies its own).
# Host keys follow the app's rule (backend/transport/ssh.py::host_key_opts): unverified unless
# RACK_SSH_KNOWN_HOSTS names a file, in which case the head's key must be in it. Mirrored here
# rather than shared because this runs on the operator's machine without the backend on its path;
# test_deploy_scripts_honours_known_hosts keeps the two in step.
if [[ -n "${RACK_SSH_KNOWN_HOSTS//[[:space:]]/}" ]]; then
  [[ "${RACK_SSH_KNOWN_HOSTS}" =~ [[:space:]] ]] && {
    echo "deploy-scripts: RACK_SSH_KNOWN_HOSTS must be a path without spaces" >&2
    exit 2
  }
  SSH_CMD="ssh -o StrictHostKeyChecking=yes -o UserKnownHostsFile=${RACK_SSH_KNOWN_HOSTS} -o IdentitiesOnly=yes"
else
  SSH_CMD="ssh -o StrictHostKeyChecking=no"
fi
[[ -n "${RACK_SSH_KEY:-}"  ]] && SSH_CMD+=" -i ${RACK_SSH_KEY}"
[[ -n "${RACK_SSH_PORT:-}" ]] && SSH_CMD+=" -p ${RACK_SSH_PORT}"

[[ -d "${SRC}" ]] || { echo "deploy-scripts: source not found: ${SRC}" >&2; exit 1; }

if [[ "${LAYOUT}" == "auto" ]]; then
  if ${SSH_CMD} "${TARGET}" "test -d ${TREE}/multi-platform" 2>/dev/null; then
    LAYOUT="tree"
  else
    LAYOUT="legacy"
  fi
fi

if [[ "${LAYOUT}" == "tree" ]]; then
  DEST="${TARGET}:${TREE}/multi-platform/"
else
  DEST="${TARGET}:${BASE%/}/bin/scripts/"
fi

# -r recursive, -l links, -p perms (keep +x), -t times, -c checksum (content-based), -i itemize.
RSYNC_COMMON=(-rlptc -i --exclude='__pycache__/' --exclude='*.pyc' --exclude='*.bak*')
# `platform/` is a source-tree convention, not a deployed directory: its contents become
# per-platform layers beside multi-platform/, so it must not be copied *into* the default set.
RSYNC_COMMON+=(--exclude='platform/')
# `custom/` belongs to the deployment, not to the repo. A deploy must never create, overwrite
# or delete it -- it holds scripts operators added on that head node, and this script is how the
# *shipped* set is updated.
RSYNC_COMMON+=(--exclude='custom/')

echo "deploy-scripts: ${SRC} -> ${DEST}  [${LAYOUT}]${DRY:+  [dry-run]}${DELETE:+  [--delete]}"
rsync "${RSYNC_COMMON[@]}" ${DRY} ${DELETE} -e "${SSH_CMD}" "${SRC}" "${DEST}"

# Platform layers: bin/scripts/platform/<id>/ -> <tree>/<id>/. Only in tree mode; in legacy mode
# there is nowhere for them to go, and `script_tree` would not look for them anyway.
if [[ "${LAYOUT}" == "tree" && -d "${REPO_ROOT}/bin/scripts/platform" ]]; then
  for dir in "${REPO_ROOT}"/bin/scripts/platform/*/; do
    [[ -d "${dir}" ]] || continue
    id="$(basename "${dir}")"
    echo "deploy-scripts: ${dir} -> ${TARGET}:${TREE}/${id}/"
    rsync "${RSYNC_COMMON[@]}" ${DRY} -e "${SSH_CMD}" "${dir}" "${TARGET}:${TREE}/${id}/"
  done
fi

echo "deploy-scripts: done${DRY:+ (dry-run — nothing written)}"
