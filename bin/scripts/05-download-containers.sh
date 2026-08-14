#!/bin/bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Junli Zhang<junliz@nvidia.com>
# Bundled for NVIS Rack Status — Tools. Original: aihpc_val installation/05-download-containers.sh
#
# Usage:
#   export NGC_API_KEY='nvapi-...'   # required only when the .sqsh is missing (skipped if output already exists)
#   ./05-download-containers.sh <destination_directory> [enroot_arch]
#
# destination_directory  — absolute or user-relative path; created if missing (mkdir -p).
# enroot_arch            — optional, default aarch64 (matches original BCM head→aarch64 image pull).
#
# The script uses a temporary ENROOT_CONFIG_PATH so credentials are not left in ~/.config/enroot.

set -euo pipefail

usage() {
  echo "Usage: NGC_API_KEY=nvapi-... ${0##*/} <destination_directory> [enroot_arch]" >&2
  echo "  enroot_arch defaults to aarch64 (set to x86_64 or host arch as needed)." >&2
  exit 1
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage
fi

_raw_dest=$1
if [[ "${_raw_dest#/}" == "$_raw_dest" ]]; then
  echo "${0##*/}: destination must be an absolute path" >&2
  exit 1
fi
ARCH="${2:-aarch64}"

mkdir -p "$_raw_dest"
DEST=$(cd "$_raw_dest" && pwd)
cd "$DEST"

NEMO_OUT="${DEST}/nemo-25.11.01.sqsh"
_skip_file=""
if [[ -f "$NEMO_OUT" && -s "$NEMO_OUT" ]]; then
  _skip_file=$NEMO_OUT
fi
if [[ -z "$_skip_file" && -n "${NEMO_SQSH_SRC:-}" ]]; then
  _bn=$(basename "${NEMO_SQSH_SRC}")
  if [[ "$_bn" == *.sqsh ]]; then
    _cand="${DEST}/${_bn}"
    if [[ -f "$_cand" && -s "$_cand" ]]; then
      _skip_file=$_cand
    fi
  fi
fi
if [[ -z "$_skip_file" && -n "${NEMO_SQSH_SRC:-}" ]]; then
  _ns=${NEMO_SQSH_SRC/#\~/$HOME}
  if [[ -f "$_ns" && -s "$_ns" && "$_ns" == *.sqsh ]]; then
    _parent=$(dirname "$(readlink -f "$_ns")")
    if [[ "$_parent" == "$DEST" ]]; then
      _skip_file=$(readlink -f "$_ns")
    fi
  fi
fi
if [[ -n "$_skip_file" ]]; then
  echo "${0##*/}: file already exists, skipping enroot import: $_skip_file"
  exit 0
fi

if [[ -z "${NGC_API_KEY:-}" ]]; then
  echo "${0##*/}: set NGC_API_KEY to your nvcr.io / NGC API key (nvapi-...)" >&2
  exit 1
fi

# Enroot's site defaults (e.g. /var/lib/enroot and /run/enroot from /etc/enroot/enroot.conf)
# require root to create, so we relocate every enroot working path. Two storage classes:
#
#   * CACHE — pulled layer blobs (plain files). NFS is fine; keep it on the destination so
#     it persists and a re-run skips the download. Override with NVIS_ENROOT_BASE.
#   * DATA / TEMP / RUNTIME — layer extraction + AUFS->OverlayFS whiteout conversion, which
#     creates device nodes via mknod. NFS rejects that ("enroot-aufs2ovlfs: failed to create
#     ovlfs whiteout: Operation not permitted"), so these MUST live on a LOCAL filesystem.
#
# Pick a local, writable, non-NFS scratch dir (prefer real disk over tmpfs/RAM, falling back
# to the one with the most free space). Override with NVIS_ENROOT_LOCAL.
_enroot_base="${NVIS_ENROOT_BASE:-${DEST}/.enroot}"
export ENROOT_CACHE_PATH="${ENROOT_CACHE_PATH:-${_enroot_base}/cache}"
mkdir -p "$ENROOT_CACHE_PATH"

_fstype() { stat -f -c %T "$1" 2>/dev/null; }
_avail_gb() { df -Pk "$1" 2>/dev/null | awk 'NR==2{print int($4/1024/1024)}'; }
_pick_local_scratch() {
  local need_gb="${NVIS_ENROOT_MIN_TMP_GB:-40}"
  local best="" best_avail=-1 d ft av
  for d in "${NVIS_ENROOT_LOCAL:-}" /var/tmp /tmp /dev/shm; do
    [ -n "$d" ] && [ -d "$d" ] && [ -w "$d" ] || continue
    ft=$(_fstype "$d"); case "$ft" in nfs*|*nfs) continue ;; esac
    av=$(_avail_gb "$d"); [ -n "$av" ] || continue
    # Explicit override always wins.
    if [ -n "${NVIS_ENROOT_LOCAL:-}" ] && [ "$d" = "${NVIS_ENROOT_LOCAL%/}" ]; then echo "$d"; return 0; fi
    # First candidate (disk before tmpfs, per loop order) with enough room wins.
    if [ "$av" -ge "$need_gb" ]; then echo "$d"; return 0; fi
    if [ "$av" -gt "$best_avail" ]; then best="$d"; best_avail="$av"; fi
  done
  [ -n "$best" ] && { echo "$best"; return 0; }
  return 1
}
_local_root="$(_pick_local_scratch || true)"
if [ -z "$_local_root" ]; then
  echo "${0##*/}: no local scratch found for enroot temp; NFS cannot create overlay whiteouts." >&2
  echo "  Set NVIS_ENROOT_LOCAL to a local, writable path with free space (e.g. /raid/scratch)." >&2
  exit 1
fi
_local_base="${_local_root%/}/nvis-enroot-$(id -u)"
export ENROOT_DATA_PATH="${ENROOT_DATA_PATH:-${_local_base}/data}"
export ENROOT_RUNTIME_PATH="${ENROOT_RUNTIME_PATH:-${_local_base}/runtime}"
export ENROOT_TEMP_PATH="${ENROOT_TEMP_PATH:-${_local_base}/tmp}"
mkdir -p "$ENROOT_DATA_PATH" "$ENROOT_RUNTIME_PATH" "$ENROOT_TEMP_PATH"
echo "${0##*/}: enroot cache=${ENROOT_CACHE_PATH} (layers), local scratch=${_local_root} ($(_fstype "$_local_root"), $(_avail_gb "$_local_root")G free)"

ENROOT_CONFIG_PATH=$(mktemp -d "${TMPDIR:-/tmp}/nvis-enroot-config.XXXXXX")
chmod 0700 "$ENROOT_CONFIG_PATH"
export ENROOT_CONFIG_PATH
cleanup() {
  # Config holds credentials (always remove). Runtime/temp are transient working dirs.
  # Keep cache/data so a re-run can reuse pulled layers instead of re-downloading.
  rm -rf "$ENROOT_CONFIG_PATH" "$ENROOT_RUNTIME_PATH" "$ENROOT_TEMP_PATH"
}
trap cleanup EXIT

cat > "$ENROOT_CONFIG_PATH/.credentials" <<EOF
machine nvcr.io login \$oauthtoken password ${NGC_API_KEY}
machine authn.nvidia.com login \$oauthtoken password ${NGC_API_KEY}
EOF
chmod 0600 "$ENROOT_CONFIG_PATH/.credentials"

## HPC benchmark Image (uncomment on site)
#enroot import \
# --arch "${ARCH}" \
# --output "${DEST}/hpc-benchmarks-25.04.sqsh" \
# 'docker://nvcr.io/nvidia/hpc-benchmarks:25.04'

#enroot import \
# --arch "${ARCH}" \
# --output "${DEST}/hpc-benchmarks-25.09.sqsh" \
# 'docker://nvcr.io/nvidia/hpc-benchmarks:25.09'

## Nemo Image (uncomment older tags if needed)
#enroot import \
# --arch "${ARCH}" \
# --output "${DEST}/nemo-25.04.rc2.sqsh" \
# 'docker://nvcr.io/nvidian/nemo:25.04.rc2'

enroot import \
  --arch "${ARCH}" \
  --output "${NEMO_OUT}" \
  'docker://nvcr.io/nvidia/nemo:25.11.01'

echo "${0##*/}: done — output under ${DEST}"
