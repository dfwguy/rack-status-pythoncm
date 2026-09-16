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
#   bin/deploy-scripts.sh [--dry-run] [--delete] [--only RELPATH ...] [user@host | ssh-alias]
#
# Target resolution (first that applies):
#   1. explicit  user@host  or an  ssh-alias  argument
#   2. RACK_SSH_USER@RACK_SSH_HOST from the environment
#
# Env (same names the app uses in SSH mode — see .cursor/rules/local-dev.mdc):
#   RACK_SSH_HOST          head node hostname/IP (required unless a target arg is given)
#   RACK_SSH_USER          explicit non-root SSH user (required)
#   RACK_SSH_KEY           SSH private key path (optional; omit when using an ssh-alias)
#   RACK_SSH_PORT          SSH port (optional; default from ssh config / 22)
#   RACK_KNOWN_HOSTS_PATH  pinned known_hosts file (required)
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
ONLY=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--dry-run) DRY="-n"; shift ;;
    --delete)     DELETE="--delete"; shift ;;
    --only)       ONLY+=("${2:?--only needs a path relative to bin/scripts}"); shift 2 ;;
    -h|--help)    grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)           echo "deploy-scripts: unknown option: $1" >&2; exit 2 ;;
    *)            TARGET="$1"; shift ;;
  esac
done

if (( ${#ONLY[@]} > 0 )) && [[ -n "${DELETE}" ]]; then
  echo "deploy-scripts: --delete cannot be combined with --only" >&2
  exit 2
fi

if [[ -z "${TARGET}" ]]; then
  [[ -n "${RACK_SSH_HOST:-}" ]] || {
    echo "deploy-scripts: no target — pass user@host / a host, or set RACK_SSH_HOST" >&2
    exit 2
  }
  TARGET="${RACK_SSH_HOST}"
fi

# A deployment must name its least-privilege identity.  Do not inherit a User
# directive from an operator's mutable ssh_config and never silently become root.
if [[ "${TARGET}" == *@* ]]; then
  TARGET_USER="${TARGET%%@*}"
  TARGET_HOST="${TARGET#*@}"
  if [[ -n "${RACK_SSH_USER:-}" && "${RACK_SSH_USER}" != "${TARGET_USER}" ]]; then
    echo "deploy-scripts: target user and RACK_SSH_USER disagree" >&2
    exit 2
  fi
else
  [[ -n "${RACK_SSH_USER:-}" ]] || {
    echo "deploy-scripts: RACK_SSH_USER is required (explicit non-root identity)" >&2
    exit 2
  }
  TARGET_USER="${RACK_SSH_USER}"
  TARGET_HOST="${TARGET}"
fi
[[ -n "${TARGET_USER}" && -n "${TARGET_HOST}" ]] || {
  echo "deploy-scripts: invalid target: ${TARGET}" >&2
  exit 2
}
[[ "${TARGET_USER}" != "root" && "${TARGET_USER}" != "0" ]] || {
  echo "deploy-scripts: root SSH deployments are forbidden; use a least-privilege service identity" >&2
  exit 2
}
TARGET="${TARGET_USER}@${TARGET_HOST}"

KNOWN_HOSTS="${RACK_KNOWN_HOSTS_PATH:-}"
[[ -n "${KNOWN_HOSTS}" ]] || {
  echo "deploy-scripts: RACK_KNOWN_HOSTS_PATH is required (pinned host keys)" >&2
  exit 2
}
[[ -f "${KNOWN_HOSTS}" && -r "${KNOWN_HOSTS}" && ! -L "${KNOWN_HOSTS}" ]] || {
  echo "deploy-scripts: known_hosts must be a readable regular non-symlink file: ${KNOWN_HOSTS}" >&2
  exit 2
}
if [[ -n "${RACK_SSH_KEY:-}" && ( ! -f "${RACK_SSH_KEY}" || ! -r "${RACK_SSH_KEY}" || -L "${RACK_SSH_KEY}" ) ]]; then
  echo "deploy-scripts: SSH key must be a readable regular non-symlink file: ${RACK_SSH_KEY}" >&2
  exit 2
fi

BASE="${RACK_REMOTE_BASE_PATH:-/home/cmsupport/nvis-rack-status}"
DEST="${TARGET}:${BASE%/}/bin/scripts/"

# Keep transport options as an argv array.  rsync's -e boundary accepts only a
# string, so serialize each already-separated argument with shell quoting there.
SSH_ARGS=(
  ssh
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=${KNOWN_HOSTS}"
)
[[ -n "${RACK_SSH_KEY:-}"  ]] && SSH_ARGS+=( -i "${RACK_SSH_KEY}" )
[[ -n "${RACK_SSH_PORT:-}" ]] && SSH_ARGS+=( -p "${RACK_SSH_PORT}" )
printf -v SSH_CMD '%q ' "${SSH_ARGS[@]}"
SSH_CMD="${SSH_CMD% }"

[[ -d "${SRC}" ]] || { echo "deploy-scripts: source not found: ${SRC}" >&2; exit 1; }

echo "deploy-scripts: ${SRC} -> ${DEST}${DRY:+  [dry-run]}${DELETE:+  [--delete]}"
# -r recursive, -l links, -p perms (keep +x), -t times, -c checksum (content-based), -i itemize.
if (( ${#ONLY[@]} > 0 )); then
  sources=()
  for rel in "${ONLY[@]}"; do
    [[ "$rel" != /* && "$rel" != *".."* && -f "${SRC}${rel}" ]] || {
      echo "deploy-scripts: unsafe or missing --only path: ${rel}" >&2
      exit 2
    }
    # The /./ anchor plus --relative preserves a nested path such as lib/foo.sh.
    sources+=("${SRC%/}/./${rel}")
  done
  printf 'deploy-scripts: selective files:'
  printf ' %s' "${ONLY[@]}"
  printf '\n'
  rsync -rlptcR ${DRY} -i -e "${SSH_CMD}" "${sources[@]}" "${DEST}"
else
  rsync -rlptc ${DRY} ${DELETE} -i \
    --exclude='__pycache__/' --exclude='*.pyc' --exclude='*.bak*' \
    -e "${SSH_CMD}" \
    "${SRC}" "${DEST}"
fi

echo "deploy-scripts: done${DRY:+ (dry-run — nothing written)}"
