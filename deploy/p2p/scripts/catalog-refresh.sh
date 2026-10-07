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
# catalog-refresh.sh -- keep this node's catalog pins current with the origin's.
# Author: Rafael M. Koike
#
# WHY: a node's downloader only accepts an image its local catalog/images.tsv pins (anything else is
# CATALOG_IMAGE_UNKNOWN). A catalog sync pushes the new pins to every node once, best-effort; a node
# that missed that push stayed stale until someone noticed -- on Nscale NC, 21 nodes deferred a whole
# campaign that way. Run on a timer (nvis-p2p-catalog-refresh.timer), this bounds how stale a node can
# be. It moves only the catalog, never image content: images still arrive through paced campaigns.
#
# Fetch, VALIDATE, then swap atomically: a truncated or garbage response must never replace a good
# catalog. The last outcome is written to run/catalog-refresh.status for the fleet census to read.
#
# Env: NVIS_P2P_CATALOG_URL (required)  the origin's images.tsv
#      NVIS_P2P_BASE (default /opt/containers/.nvis-p2p)
set -euo pipefail

base=${NVIS_P2P_BASE:-/opt/containers/.nvis-p2p}
url=${NVIS_P2P_CATALOG_URL:?NVIS_P2P_CATALOG_URL is required}
target=$base/catalog/images.tsv
status=$base/run/catalog-refresh.status

mkdir -p "$base/catalog" "$base/run"
tmp=$(mktemp "$base/catalog/.images.tsv.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT

report() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" > "$status.tmp"
  mv -f "$status.tmp" "$status"
  echo "$*"
}

if ! curl -fsS --max-time 60 -o "$tmp" "$url"; then
  report "CATALOG_REFRESH_FAILED reason=fetch url=$url"
  exit 1
fi

# A response that does not end in a newline was cut off mid-line -- and node-side `while read`
# consumers would silently skip its last image even if the line were whole. Reject it.
if [[ -n $(tail -c 1 "$tmp") ]]; then
  report "CATALOG_REFRESH_REJECTED reason=invalid-catalog detail=no-final-newline url=$url"
  exit 1
fi

# The shape catalog sync writes: an exact header, then order, name, *.sqsh filename, bytes, and a
# sha256 that is 64 lowercase hex or blank (an image added but not yet pinned). At least one image.
# (No regex intervals: node awks vary, so the sha length is checked with length().)
if ! awk -F'\t' '
  NR == 1 { if ($0 != "order\tname\tfilename\tbytes\tsha256") bad = 1; next }
  NF != 5 || $1 !~ /^[0-9]+$/ || $3 !~ /^[A-Za-z0-9][A-Za-z0-9._+-]*\.sqsh$/ || $4 !~ /^[0-9]+$/ { bad = 1 }
  $5 != "" && (length($5) != 64 || $5 !~ /^[0-9a-f]+$/) { bad = 1 }
  END { exit (bad || NR < 2) ? 1 : 0 }' "$tmp"; then
  report "CATALOG_REFRESH_REJECTED reason=invalid-catalog url=$url"
  exit 1
fi

rows=$(( $(wc -l < "$tmp") - 1 ))
if [[ -f $target ]] && cmp -s "$tmp" "$target"; then
  report "CATALOG_REFRESH_CURRENT rows=$rows"
  exit 0
fi
chmod 0644 "$tmp"
mv -f "$tmp" "$target"
report "CATALOG_REFRESH_UPDATED rows=$rows"
