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
# 06-download-mnubergemm.sh — stage the MNUBERGEMM container for NscaleTX.
#
# Companion to 05-download-containers.sh (which is NeMo-only and hardcoded).
# Wraps the canonical MOP §7.2 import:
#
#   enroot import -o /home/cmods/bringup/containers/mnubergemm-26.03-v1.17-arm.sqsh \
#     docker://nvcr.io/0948245400141054/mnubergemm:tensorrt-26.03-v1.17-arm
#
# adding only what that one-liner needs to survive in this environment:
#   * idempotent skip when the .sqsh is already staged
#   * explicit NGC_API_KEY check with an actionable message (never a silent failure)
#   * credentials written to a private, auto-deleted enroot config dir
#   * enroot DATA/TEMP forced onto LOCAL disk — /cm/shared and /home are NFS, which cannot
#     create the overlayfs whiteout device nodes enroot needs ("Operation not permitted")
#   * atomic .partial -> final rename so an interrupted pull never leaves a half image
#     that the idempotent skip would later treat as good
#
# Usage:
#   NGC_API_KEY=nvapi-... ./06-download-mnubergemm.sh [DEST_DIR] [TAG] [ARCH]
#   NGC_API_KEY=nvapi-... ./06-download-mnubergemm.sh --list      # published tags
#
# Defaults match the NscaleTX layout (AAT_HOME=/home/cmods/bringup).
set -uo pipefail

REGISTRY="${MNUBERGEMM_REGISTRY:-nvcr.io}"
REPO="${MNUBERGEMM_REPO:-0948245400141054/mnubergemm}"

log()  { printf '%s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || die "curl is required"

# Docker Registry v2 auth: nvcr.io replies 401 with a WWW-Authenticate challenge naming a
# token realm+service. curl -u alone does NOT follow that, so we do the two-step properly:
# basic-auth to the realm for a bearer token, then the real request with that token.
# Falls back to plain basic auth for registries that accept it directly.
_tags() {
  local url="https://${REGISTRY}/v2/${REPO}/tags/list"
  local hdrs realm service token

  hdrs="$(curl -sS -o /dev/null -D - --max-time 30 "${url}" 2>/dev/null)"
  realm="$(sed -n 's/.*[Ww]ww-[Aa]uthenticate:.*realm="\([^"]*\)".*/\1/p'   <<<"${hdrs}" | head -1)"
  service="$(sed -n 's/.*[Ww]ww-[Aa]uthenticate:.*service="\([^"]*\)".*/\1/p' <<<"${hdrs}" | head -1)"

  if [[ -n "${realm}" ]]; then
    token="$(curl -sS --max-time 45 -u "\$oauthtoken:${NGC_API_KEY}" \
      --get --data-urlencode "service=${service}" \
            --data-urlencode "scope=repository:${REPO}:pull" \
      "${realm}" 2>/dev/null | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
print(d.get('token') or d.get('access_token') or '')
" 2>/dev/null)"
    if [[ -n "${token}" ]]; then
      curl -fsSL --max-time 45 -H "Authorization: Bearer ${token}" "${url}" 2>/dev/null && return 0
    fi
  fi

  curl -fsSL --max-time 45 -u "\$oauthtoken:${NGC_API_KEY}" "${url}" 2>/dev/null
}

if [[ "${1:-}" == "--list" ]]; then
  [[ -n "${NGC_API_KEY:-}" ]] || die "NGC_API_KEY must be set for --list"
  log "== ${REGISTRY}/${REPO}"
  _tags | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('   (no/!json response — check the key and repo)'); raise SystemExit
tags=sorted(d.get('tags') or [])
print('   total:',len(tags))
for t in tags:
    print('    ', t, '  <-- arm' if t.endswith('-arm') else '')
" || die "could not list tags for ${REGISTRY}/${REPO}"
  exit 0
fi

DEST_DIR="${1:-/home/cmods/bringup/containers}"
TAG="${2:-tensorrt-26.03-v1.17-arm}"
ARCH="${3:-arm64}"

case "${ARCH}" in
  arm64|aarch64) ARCH=arm64 ;;
  amd64|x86_64)  ARCH=amd64 ;;
  none|-)        ARCH="" ;;      # let enroot take the manifest as-is
  *) die "ARCH must be arm64, amd64, or none (got '${ARCH}')" ;;
esac
[[ "${ARCH}" == "amd64" ]] && warn "ARCH=amd64 — GB300 trays are Grace/ARM64; this will not run on them."

[[ "${DEST_DIR#/}" != "${DEST_DIR}" ]] || die "DEST_DIR must be an absolute path"
mkdir -p "${DEST_DIR}" || die "cannot create ${DEST_DIR}"
DEST_DIR="$(cd "${DEST_DIR}" && pwd)"

# Output name follows the MOP's: version-tag with the 'tensorrt-' prefix dropped, which is
# also what AAT expects (${AAT_HOME}/containers/mnubergemm-26.03-v1.17-arm.sqsh).
OUT="${MNUBERGEMM_OUT:-${DEST_DIR}/mnubergemm-${TAG#tensorrt-}.sqsh}"

if [[ "${MNUBERGEMM_FORCE:-0}" != "1" && -s "${OUT}" ]]; then
  log "Already staged, skipping import: ${OUT}"
  ls -lh "${OUT}"
  exit 0
fi

if [[ -z "${NGC_API_KEY:-}" ]]; then
  die "NGC_API_KEY is not set (nvapi-...).
       In Rack Status: Settings -> External connections -> 'NGC API key'.
       It is forwarded to the head node automatically in SSH exec mode.
       Nothing was downloaded."
fi

command -v enroot >/dev/null 2>&1 || die "enroot not found — run this on the BCM head node"

# --- enroot paths: CACHE may be NFS, DATA/TEMP/RUNTIME must be local -----------------
_fstype()  { stat -f -c %T "$1" 2>/dev/null; }
_avail_gb() { df -Pk "$1" 2>/dev/null | awk 'NR==2{print int($4/1024/1024)}'; }
_pick_local() {
  local best="" best_av=-1 d ft av
  for d in "${NVIS_ENROOT_LOCAL:-}" /var/tmp /tmp; do
    [[ -n "${d}" && -d "${d}" && -w "${d}" ]] || continue
    ft="$(_fstype "${d}")"
    case "${ft}" in nfs*|autofs) continue ;; esac
    av="$(_avail_gb "${d}")"; [[ -z "${av}" ]] && av=0
    (( av > best_av )) && { best_av="${av}"; best="${d}"; }
  done
  [[ -n "${best}" ]] && printf '%s' "${best}"
}
LOCAL_ROOT="$(_pick_local)" || true
[[ -n "${LOCAL_ROOT}" ]] || die "no local (non-NFS) scratch dir found for enroot extraction"
(( $(_avail_gb "${LOCAL_ROOT}") >= 40 )) || warn "only $(_avail_gb "${LOCAL_ROOT}")G free on ${LOCAL_ROOT} (~40G recommended)"

BASE="${LOCAL_ROOT}/nvis-enroot-$(id -u)"
export ENROOT_CACHE_PATH="${ENROOT_CACHE_PATH:-${BASE}/cache}"
export ENROOT_DATA_PATH="${BASE}/data"
export ENROOT_TEMP_PATH="${BASE}/tmp"
export ENROOT_RUNTIME_PATH="${BASE}/run"
export ENROOT_CONFIG_PATH="${BASE}/cfg"
mkdir -p "${ENROOT_CACHE_PATH}" "${ENROOT_DATA_PATH}" "${ENROOT_TEMP_PATH}" \
         "${ENROOT_RUNTIME_PATH}" "${ENROOT_CONFIG_PATH}" || die "cannot create enroot dirs under ${BASE}"
chmod 700 "${ENROOT_CONFIG_PATH}"

CREDS="${ENROOT_CONFIG_PATH}/.credentials"
umask 077
printf 'machine %s login $oauthtoken password %s\n' "${REGISTRY}" "${NGC_API_KEY}" > "${CREDS}"
chmod 600 "${CREDS}"
trap 'rm -f "${CREDS}"' EXIT INT TERM

log ""
log "image    : ${REGISTRY}/${REPO}:${TAG}"
log "arch     : ${ARCH:-<manifest default>}"
log "output   : ${OUT}"
log "cache    : ${ENROOT_CACHE_PATH}"
log "scratch  : ${LOCAL_ROOT} ($(_fstype "${LOCAL_ROOT}"), $(_avail_gb "${LOCAL_ROOT}")G free)"
log ""

TMP_OUT="${OUT}.partial.$$"
arch_args=(); [[ -n "${ARCH}" ]] && arch_args=(-a "${ARCH}")

if ! enroot import "${arch_args[@]}" -o "${TMP_OUT}" "docker://${REGISTRY}#${REPO}:${TAG}"; then
  rm -f "${TMP_OUT}"
  warn "import failed. If the failure mentions the manifest/platform, the tag is likely"
  warn "single-arch already — retry with ARCH 'none':"
  warn "  ${0##*/} '${DEST_DIR}' '${TAG}' none"
  warn "To see what is published:  ${0##*/} --list"
  die "enroot import failed for ${REGISTRY}/${REPO}:${TAG}"
fi
[[ -s "${TMP_OUT}" ]] || { rm -f "${TMP_OUT}"; die "import produced an empty file"; }
mv -f "${TMP_OUT}" "${OUT}" || die "could not move ${TMP_OUT} -> ${OUT}"
chmod 644 "${OUT}" 2>/dev/null || true

log ""
log "OK — staged:"
ls -lh "${OUT}"
log ""
log "  CONT=${OUT}"
