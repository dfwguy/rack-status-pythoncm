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
# under RACK_REMOTE_BASE_PATH/bin/scripts. This wraps a checksum-based rsync so "sync scripts
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
#   RACK_REMOTE_BASE_PATH  remote install root (default: /home/cmsupport/nvis-rack-status)
#
# Examples:
#   bin/deploy-scripts.sh boston-slogin              # via an ~/.ssh/config alias
#   bin/deploy-scripts.sh --dry-run boston-slogin    # preview what would change
#   RACK_SSH_HOST=10.0.0.5 RACK_SSH_KEY=~/.ssh/id bin/deploy-scripts.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${REPO_ROOT}/bin/scripts/"

DRY=""
DELETE=""
TARGET=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--dry-run) DRY="-n"; shift ;;
    --delete)     DELETE="--delete"; shift ;;
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
DEST="${TARGET}:${BASE%/}/bin/scripts/"

# SSH transport for rsync. Key/port only when explicitly set (an ssh-alias supplies its own).
SSH_CMD="ssh -o StrictHostKeyChecking=no"
[[ -n "${RACK_SSH_KEY:-}"  ]] && SSH_CMD+=" -i ${RACK_SSH_KEY}"
[[ -n "${RACK_SSH_PORT:-}" ]] && SSH_CMD+=" -p ${RACK_SSH_PORT}"

[[ -d "${SRC}" ]] || { echo "deploy-scripts: source not found: ${SRC}" >&2; exit 1; }

echo "deploy-scripts: ${SRC} -> ${DEST}${DRY:+  [dry-run]}${DELETE:+  [--delete]}"
# -r recursive, -l links, -p perms (keep +x), -t times, -c checksum (content-based), -i itemize.
rsync -rlptc ${DRY} ${DELETE} -i \
  --exclude='__pycache__/' --exclude='*.pyc' --exclude='*.bak*' \
  -e "${SSH_CMD}" \
  "${SRC}" "${DEST}"

echo "deploy-scripts: done${DRY:+ (dry-run — nothing written)}"
