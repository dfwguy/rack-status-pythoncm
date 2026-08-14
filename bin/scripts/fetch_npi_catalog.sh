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
# fetch_npi_catalog.sh — sync the NVIDIA NPI Knowledge Portal tool catalog (TOML records)
#   into the local snapshot dir so `catalog.npi_import` can mirror it into the NVIS catalog.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# The NPI team curates the canonical validation/perf test catalog at
#   gitlab-master.nvidia.com/pshamis/NPI-Knowledge-Portal :: reports/catalog/tools/*.toml
# This pulls those records (shallow) and stages them at:
#   ${NVIS_NPI_CATALOG_DIR:-<repo>/var/cache/npi_catalog/tools}
#
# Auth (pick one):
#   * NPI_GITLAB_TOKEN=<PAT with read_repository>   -> HTTPS token clone (works headless)
#   * otherwise falls back to SSH over port 12051 using your ssh-agent / on-disk keys
#     (gitlab-master's SSH endpoint is on 12051, NOT 22 — cloning port 22 will fail)
#
# Overrides:
#   NPI_CATALOG_PROJECT   default pshamis/NPI-Knowledge-Portal
#   NPI_CATALOG_REF       default main
#   NPI_CATALOG_SUBPATH   default reports/catalog/tools
#   NPI_GITLAB_SSH_PORT   default 12051
#   NVIS_NPI_CATALOG_DIR  destination dir (default var/cache/npi_catalog/tools)
set -euo pipefail

GITLAB_HOST="gitlab-master.nvidia.com"
PROJECT="${NPI_CATALOG_PROJECT:-pshamis/NPI-Knowledge-Portal}"
REF="${NPI_CATALOG_REF:-main}"
SUBPATH="${NPI_CATALOG_SUBPATH:-reports/catalog/tools}"
SSH_PORT="${NPI_GITLAB_SSH_PORT:-12051}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
dest="${NVIS_NPI_CATALOG_DIR:-${repo_root}/var/cache/npi_catalog/tools}"

if [[ -n "${NPI_GITLAB_TOKEN:-}" ]]; then
  clone_url="https://oauth2:${NPI_GITLAB_TOKEN}@${GITLAB_HOST}/${PROJECT}.git"
  auth_mode="https-token"
else
  clone_url="ssh://git@${GITLAB_HOST}:${SSH_PORT}/${PROJECT}.git"
  auth_mode="ssh:${SSH_PORT}"
fi

echo "[fetch_npi_catalog] project=${PROJECT} ref=${REF} auth=${auth_mode}"
echo "[fetch_npi_catalog] dest=${dest}"

tmp="$(mktemp -d "${TMPDIR:-/tmp}/npi-catalog.XXXXXX")"
trap 'rm -rf "${tmp}"' EXIT

# Shallow, sparse checkout of just the catalog subpath keeps this fast and small.
git clone --depth 1 --filter=blob:none --sparse --branch "${REF}" "${clone_url}" "${tmp}/repo" >/dev/null 2>&1 || {
  echo "[fetch_npi_catalog] ERROR: clone failed. Check auth (NPI_GITLAB_TOKEN or SSH) and network." >&2
  exit 1
}
git -C "${tmp}/repo" sparse-checkout set "${SUBPATH}" >/dev/null 2>&1

src="${tmp}/repo/${SUBPATH}"
if [[ ! -d "${src}" ]]; then
  echo "[fetch_npi_catalog] ERROR: ${SUBPATH} not found in ${PROJECT}@${REF}" >&2
  exit 1
fi

count="$(find "${src}" -maxdepth 1 -name '*.toml' | wc -l | tr -d ' ')"
if [[ "${count}" == "0" ]]; then
  echo "[fetch_npi_catalog] ERROR: no *.toml records found under ${SUBPATH}" >&2
  exit 1
fi

# Atomically swap the destination so a reader never sees a partial sync.
mkdir -p "$(dirname "${dest}")"
staging="${dest}.staging.$$"
rm -rf "${staging}"
mkdir -p "${staging}"
cp "${src}"/*.toml "${staging}/"
commit="$(git -C "${tmp}/repo" rev-parse --short HEAD 2>/dev/null || echo unknown)"
cat > "${staging}/MANIFEST.txt" <<EOF
source=${GITLAB_HOST}/${PROJECT}
ref=${REF}
commit=${commit}
subpath=${SUBPATH}
fetched=$(date -u +%Y-%m-%dT%H:%M:%SZ)
records=${count}
EOF
rm -rf "${dest}"
mv "${staging}" "${dest}"

echo "[fetch_npi_catalog] synced ${count} tool records (commit ${commit}) -> ${dest}"
echo "[fetch_npi_catalog] next: POST /api/catalog/npi-tools/import (or restart the app to auto-import on boot)"
