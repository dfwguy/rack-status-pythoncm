#!/bin/bash
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

ENROOT_CONFIG_PATH=$(mktemp -d "${TMPDIR:-/tmp}/nvis-enroot-config.XXXXXX")
chmod 0700 "$ENROOT_CONFIG_PATH"
export ENROOT_CONFIG_PATH
cleanup() {
  rm -rf "$ENROOT_CONFIG_PATH"
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
