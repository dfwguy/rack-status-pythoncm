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
# NVIS Rack Status — DCGM / DCGMI workspace installer (shared filesystem model).
#
# Runs on the dashboard host (BCM head / slogin). Downloads the NVIDIA Data
# Center GPU Manager (DCGM 4.x) package files and EXTRACTS them into the shared
# workspace directory (NFS /home) instead of installing system-wide. Because the
# workspace is shared, every compute node sees the same binaries — no per-node
# root install needed. The compute nodes just execute the extracted binary.
#
# The mnubergemm/ubergemm multi-node GEMM stress binary ships inside the DCGM
# cuda-major plugin packages from DCGM 4.3.0 onwards; this installer makes it
# available at $WORKSPACE/dcgm/current and exports NVIS_MNUBERGEMM_BIN for the
# stress sbatch (bin/scripts/mnubergemm_stress.sbatch honors that override).
#
# Usage: install-dcgm-workspace.sh [WORKSPACE] [CUDA_MAJOR] [ARCH]
#   WORKSPACE    shared workspace root (default: the cluster's workspace, NVIS_WORKSPACE_DIR)
#   CUDA_MAJOR   12 | 13   selects datacenter-gpu-manager-4-cudaXX (default: 13)
#   ARCH         TARGET COMPUTE-NODE arch — NOT this head node's arch:
#                  aarch64 (GBx00 / Grace-Blackwell, ARM)  [default]
#                  x86_64  (Bx00, x86)
#
# Environment:
#   NVIS_DCGM_DISTRO   dgx | ubuntu2404 | ubuntu2204 | ubuntu2004 | debian12 |
#                      rhel9 | rhel8 | sles15        (default: dgx -> ubuntu2404, .deb)
#   NVIS_DCGM_VERSION  optional version substring to pin (e.g. 4.5.3)
#   NVIS_DCGM_FORCE    0 | 1   re-extract even if already present  (default: 0)
set -euo pipefail

# Workspace = the explicit argument, else the cluster's workspace (project setting
# connection.workspace_dir -> NVIS_WORKSPACE_DIR); workspace-dir-common.sh fails naming the setting.
WORKSPACE_DIR="${1:-}"
# shellcheck source=workspace-dir-common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/workspace-dir-common.sh" || exit 2
WORKSPACE="${WORKSPACE_DIR}"
CUDA_MAJ="${2:-13}"
ARCH_IN="${3:-aarch64}"
DISTRO_IN="${NVIS_DCGM_DISTRO:-dgx}"
VERSION="${NVIS_DCGM_VERSION:-}"
FORCE="${NVIS_DCGM_FORCE:-0}"

log() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

case "${CUDA_MAJ}" in 12|13) ;; *) die "CUDA_MAJOR must be 12 or 13 (got '${CUDA_MAJ}')";; esac

# --- target arch (the compute node arch we download binaries for) ---
case "${ARCH_IN}" in
  aarch64|arm64|sbsa) REPO_ARCH="sbsa";   DEB_ARCH="arm64"; RPM_ARCH="aarch64"; GEN_LABEL="aarch64 (GBx00 / Grace-Blackwell, ARM)";;
  x86_64|amd64)       REPO_ARCH="x86_64"; DEB_ARCH="amd64"; RPM_ARCH="x86_64";  GEN_LABEL="x86_64 (Bx00, x86)";;
  *) die "ARCH must be aarch64 or x86_64 (got '${ARCH_IN}')";;
esac

# --- Linux flavor -> NVIDIA CUDA repo path + package kind ---
PKG_KIND=""   # deb | rpm
case "${DISTRO_IN}" in
  dgx|ubuntu2404) REPO_DISTRO=ubuntu2404; PKG_KIND=deb;;   # NVIDIA DGX OS is Ubuntu-based (newest)
  ubuntu2204)     REPO_DISTRO=ubuntu2204; PKG_KIND=deb;;
  ubuntu2004)     REPO_DISTRO=ubuntu2004; PKG_KIND=deb;;
  debian12)       REPO_DISTRO=debian12;   PKG_KIND=deb;;
  rhel9)          REPO_DISTRO=rhel9;       PKG_KIND=rpm;;
  rhel8)          REPO_DISTRO=rhel8;       PKG_KIND=rpm;;
  sles15)         REPO_DISTRO=sles15;      PKG_KIND=rpm;;
  *) die "unknown NVIS_DCGM_DISTRO '${DISTRO_IN}'";;
esac

BASE="https://developer.download.nvidia.com/compute/cuda/repos/${REPO_DISTRO}/${REPO_ARCH}"

# --- downloader helper (curl preferred, wget fallback) ---
if command -v curl >/dev/null 2>&1; then
  _fetch() { curl -fSL --retry 3 -o "$1" "$2"; }
  _list()  { curl -fsSL "$1"; }
elif command -v wget >/dev/null 2>&1; then
  _fetch() { wget -q -O "$1" "$2"; }
  _list()  { wget -qO- "$1"; }
else
  die "neither curl nor wget is available on this host"
fi

PKG_NAMES=(
  "datacenter-gpu-manager-4-core"
  "datacenter-gpu-manager-4-cuda${CUDA_MAJ}"
  "datacenter-gpu-manager-4-proprietary-cuda${CUDA_MAJ}"
)

DEST="${WORKSPACE%/}/dcgm"
PKGDIR="${DEST}/pkgs"
ROOT="${DEST}/root"
ENV_FILE="${DEST}/dcgm-env.sh"

log "===================================================================="
log " NVIS DCGM / DCGMI workspace install (shared filesystem)"
log "===================================================================="
log "workspace    : ${WORKSPACE}"
log "target arch  : ${REPO_ARCH}  ->  ${GEN_LABEL}"
log "flavor       : ${DISTRO_IN}  ->  repo ${REPO_DISTRO} (${PKG_KIND})"
log "cuda major   : ${CUDA_MAJ}  (datacenter-gpu-manager-4-cuda${CUDA_MAJ})"
[ -n "${VERSION}" ] && log "version pin  : ${VERSION}"
log "repo base    : ${BASE}"
log "extract to   : ${ROOT}"
log ""

# --- locate ubergemm/mnubergemm inside the extracted tree ---
_find_ubergemm() {
  local cu name cand
  for cu in "cuda${CUDA_MAJ}" cuda13 cuda12; do
    for name in updated/ubergemm updated/mnubergemm ubergemm mnubergemm; do
      cand="${ROOT}/usr/libexec/datacenter-gpu-manager-4/plugins/${cu}/${name}"
      if [ -x "${cand}" ]; then printf '%s\n' "${cand}"; return 0; fi
    done
  done
  return 1
}

_write_env() {
  local ug="$1"
  local triplet
  case "${REPO_ARCH}" in sbsa) triplet="aarch64-linux-gnu";; *) triplet="x86_64-linux-gnu";; esac
  cat > "${ENV_FILE}" <<EOF
# Generated by install-dcgm-workspace.sh — source to use the workspace DCGM build.
export DCGM_HOME="${ROOT}/usr"
export PATH="${ROOT}/usr/bin:\${PATH}"
export LD_LIBRARY_PATH="${ROOT}/usr/lib/${triplet}:${ROOT}/usr/lib:${ROOT}/usr/lib64:\${LD_LIBRARY_PATH:-}"
export NVIS_MNUBERGEMM_BIN="${ug}"
EOF
  ln -sfn "${ROOT}" "${DEST}/current" 2>/dev/null || true
}

if [ "${FORCE}" != "1" ]; then
  _existing="$(_find_ubergemm || true)"
  if [ -n "${_existing}" ]; then
    log "Already extracted: ${_existing}"
    log "Set NVIS_DCGM_FORCE=1 to re-download/extract. Re-writing env file only."
    _write_env "${_existing}"
    exit 0
  fi
fi

mkdir -p "${PKGDIR}" "${ROOT}"

# --- portable .deb extractor (dpkg-deb, else ar + tar with zstd/xz/gz) ---
_extract_deb() {
  local deb="$1" dest="$2"
  if command -v dpkg-deb >/dev/null 2>&1; then
    dpkg-deb -x "${deb}" "${dest}"
    return $?
  fi
  command -v ar >/dev/null 2>&1 || die "cannot extract .deb: need dpkg-deb or ar"
  local tmp; tmp="$(mktemp -d)"
  ( cd "${tmp}" && ar x "${deb}" )
  local data; data="$(ls "${tmp}"/data.tar.* 2>/dev/null | head -1)"
  [ -n "${data}" ] || die "no data.tar.* inside ${deb}"
  case "${data}" in
    *.zst) command -v zstd >/dev/null 2>&1 || die "need zstd to extract ${data}"; zstd -dc "${data}" | tar -x -C "${dest}";;
    *.xz)  tar -xJf "${data}" -C "${dest}";;
    *.gz)  tar -xzf "${data}" -C "${dest}";;
    *)     tar -xf "${data}" -C "${dest}";;
  esac
  rm -rf "${tmp}"
}

_extract_rpm() {
  local rpm="$1" dest="$2"
  command -v rpm2cpio >/dev/null 2>&1 || die "cannot extract .rpm: need rpm2cpio"
  command -v cpio >/dev/null 2>&1 || die "cannot extract .rpm: need cpio"
  ( cd "${dest}" && rpm2cpio "${rpm}" | cpio -idm --quiet )
}

log "==> listing ${BASE}/"
IDX="$(_list "${BASE}/" || true)"
[ -n "${IDX}" ] || die "could not list repo directory ${BASE}/ (check network/VPN reachability)"

for p in "${PKG_NAMES[@]}"; do
  if [ "${PKG_KIND}" = "deb" ]; then
    if [ -n "${VERSION}" ]; then
      fname="$(printf '%s\n' "${IDX}" | grep -oE "${p}_[^\"'>]*${VERSION}[^\"'>]*_(${DEB_ARCH}|all)\.deb" | sort -V | tail -1)"
    else
      fname="$(printf '%s\n' "${IDX}" | grep -oE "${p}_[^\"'>]+_(${DEB_ARCH}|all)\.deb" | sort -V | tail -1)"
    fi
    [ -n "${fname}" ] || die "no .deb for '${p}' (${DEB_ARCH}) at ${BASE}/"
  else
    if [ -n "${VERSION}" ]; then
      fname="$(printf '%s\n' "${IDX}" | grep -oE "${p}-[^\"'>]*${VERSION}[^\"'>]*\.(${RPM_ARCH}|noarch)\.rpm" | sort -V | tail -1)"
    else
      fname="$(printf '%s\n' "${IDX}" | grep -oE "${p}-[0-9][^\"'>]+\.(${RPM_ARCH}|noarch)\.rpm" | sort -V | tail -1)"
    fi
    [ -n "${fname}" ] || die "no .rpm for '${p}' (${RPM_ARCH}) at ${BASE}/"
  fi
  log "==> ${p}: ${fname}"
  if [ ! -s "${PKGDIR}/${fname}" ]; then
    _fetch "${PKGDIR}/${fname}" "${BASE}/${fname}" || die "download ${fname} failed"
  else
    log "    (cached)"
  fi
  log "    extracting -> ${ROOT}"
  if [ "${PKG_KIND}" = "deb" ]; then _extract_deb "${PKGDIR}/${fname}" "${ROOT}"; else _extract_rpm "${PKGDIR}/${fname}" "${ROOT}"; fi
done

UG="$(_find_ubergemm || true)"
[ -n "${UG}" ] || die "ubergemm/mnubergemm not found in extracted tree under ${ROOT}/usr/libexec/datacenter-gpu-manager-4/plugins/cuda${CUDA_MAJ}"

_write_env "${UG}"

log ""
log "==> verification"
if [ -x "${ROOT}/usr/bin/dcgmi" ]; then
  log "dcgmi binary  : ${ROOT}/usr/bin/dcgmi"
fi
log "ubergemm/mnubergemm : ${UG}"
log "env file      : ${ENV_FILE}"
log ""
log "OK: DCGM ${CUDA_MAJ} extracted to ${ROOT} (target arch ${REPO_ARCH})."
log "To use on compute nodes, the stress runner can pick it up via:"
log "    source ${ENV_FILE}        # sets PATH/LD_LIBRARY_PATH + NVIS_MNUBERGEMM_BIN"
log "or pass NVIS_MNUBERGEMM_BIN=${UG} to bin/scripts/mnubergemm_stress.sbatch."
