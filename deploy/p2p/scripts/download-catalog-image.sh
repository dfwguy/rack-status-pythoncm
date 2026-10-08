#!/usr/bin/env bash
set -euo pipefail

# download-catalog-image.sh FILENAME -- fetch one catalog image through the local peer, verify it
# against the catalog's sha256, and install it with a receipt. Runs on a compute node, started by
# launch-catalog-image-node.sh.
#
# Env:
#   NVIS_P2P_DOWNLOAD_TIMEOUT_SECONDS  hard limit for the transfer (default 3600). dfget is told the
#       same limit, but it is enforced from outside as well: on NC a dfget sat for ten hours past its
#       own --timeout. After the limit dfget is sent TERM, and KILL a minute later.
#   NVIS_P2P_ORIGIN  the seed origin (http://host:port). Default: derived from the catalog URL the
#       node's catalog refresh uses (catalog-refresh.env), so no site address lives in this script.
#   NVIS_P2P_BASE    the peer root (default /opt/containers/.nvis-p2p); images land beside it.
base=${NVIS_P2P_BASE:-/opt/containers/.nvis-p2p}
catalog=$base/catalog/images.tsv
filename=${1:?canonical image filename is required}
download_timeout=${NVIS_P2P_DOWNLOAD_TIMEOUT_SECONDS:-3600}
[[ $download_timeout =~ ^[1-9][0-9]*$ ]] || {
  echo "CATALOG_BAD_TIMEOUT value=$download_timeout" >&2; exit 64; }

origin=${NVIS_P2P_ORIGIN:-}
if [[ -z $origin && -r $base/catalog-refresh.env ]]; then
  catalog_url=$(sed -n 's/^NVIS_P2P_CATALOG_URL=//p' "$base/catalog-refresh.env" | head -n 1)
  [[ $catalog_url =~ ^(https?://[^/]+) ]] && origin=${BASH_REMATCH[1]}
fi
[[ $origin =~ ^https?://[A-Za-z0-9.:-]+$ ]] || {
  echo "CATALOG_ORIGIN_UNKNOWN set NVIS_P2P_ORIGIN or install the catalog refresh (catalog-refresh.env)" >&2
  exit 64; }

row=$(awk -F '\t' -v image="$filename" '$3 == image {print; exit}' "$catalog")
[[ -n $row ]] || { echo "CATALOG_IMAGE_UNKNOWN filename=$filename" >&2; exit 64; }
IFS=$'\t' read -r order display_name filename expected_bytes expected_sha <<< "$row"

target=${base%/*}/$filename
receipt=$base/receipts/$filename.sha256
partial=$base/staging/$filename.partial
mkdir -p "$base/receipts" "$base/staging" "$base/quarantine" "$base/logs"

if [[ -f $target && $(stat -c %s "$target") == "$expected_bytes" ]]; then
  if [[ -s $receipt ]] && grep -Fxq "$expected_sha  $target" "$receipt"; then
    echo "CATALOG_IMAGE_READY filename=$filename verification=receipt"
    exit 0
  fi
  actual_sha=$(ionice -c2 -n7 nice -n 10 sha256sum "$target" | awk '{print $1}')
  if [[ $actual_sha == "$expected_sha" ]]; then
    printf '%s  %s\n' "$expected_sha" "$target" > "$receipt"
    echo "CATALOG_IMAGE_READY filename=$filename verification=sha256"
    exit 0
  fi
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
if [[ -e $partial ]]; then
  mv "$partial" "$base/quarantine/$filename.partial.$stamp"
fi

rc=0
timeout --kill-after=60 "$download_timeout" ionice -c2 -n7 nice -n 10 "$base/bin/dfget" \
  --endpoint "$base/run/dfdaemon.sock" \
  --no-progress \
  --force-hard-link \
  --timeout "${download_timeout}s" \
  --priority 6 \
  --digest "sha256:$expected_sha" \
  --content-for-calculating-task-id "nvis-p2p-sha256:$expected_sha" \
  --application NVIS-P2P-Catalog \
  --tag "catalog-v1-$order" \
  --disable-back-to-source \
  "$origin/$filename" \
  -O "$partial" \
  --console || rc=$?
if (( rc == 124 || rc == 137 )); then
  echo "CATALOG_DOWNLOAD_TIMEOUT filename=$filename seconds=$download_timeout"
  exit 124
fi
(( rc == 0 )) || exit "$rc"

[[ $(stat -c %s "$partial") == "$expected_bytes" ]]
actual_sha=$(sha256sum "$partial" | awk '{print $1}')
[[ $actual_sha == "$expected_sha" ]]

if [[ -e $target ]]; then
  mv "$target" "$base/quarantine/$filename.pre-nvis-p2p.$stamp"
fi
mv "$partial" "$target"
printf '%s  %s\n' "$expected_sha" "$target" > "$receipt"
echo "CATALOG_IMAGE_INSTALLED filename=$filename bytes=$expected_bytes sha256=$expected_sha"
