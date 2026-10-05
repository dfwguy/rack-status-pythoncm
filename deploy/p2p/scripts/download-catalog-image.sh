#!/usr/bin/env bash
set -euo pipefail

base=/opt/containers/.nvis-p2p
catalog=$base/catalog/images.tsv
filename=${1:?canonical image filename is required}
origin=http://10.41.112.3:12130

row=$(awk -F '\t' -v image="$filename" '$3 == image {print; exit}' "$catalog")
[[ -n $row ]] || { echo "CATALOG_IMAGE_UNKNOWN filename=$filename" >&2; exit 64; }
IFS=$'\t' read -r order display_name filename expected_bytes expected_sha <<< "$row"

target=/opt/containers/$filename
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

ionice -c2 -n7 nice -n 10 "$base/bin/dfget" \
  --endpoint "$base/run/dfdaemon.sock" \
  --no-progress \
  --force-hard-link \
  --timeout 6h \
  --priority 6 \
  --digest "sha256:$expected_sha" \
  --content-for-calculating-task-id "nvis-p2p-sha256:$expected_sha" \
  --application NVIS-P2P-Catalog \
  --tag "catalog-v1-$order" \
  --disable-back-to-source \
  "$origin/$filename" \
  -O "$partial" \
  --console

[[ $(stat -c %s "$partial") == "$expected_bytes" ]]
actual_sha=$(sha256sum "$partial" | awk '{print $1}')
[[ $actual_sha == "$expected_sha" ]]

if [[ -e $target ]]; then
  mv "$target" "$base/quarantine/$filename.pre-nvis-p2p.$stamp"
fi
mv "$partial" "$target"
printf '%s  %s\n' "$expected_sha" "$target" > "$receipt"
echo "CATALOG_IMAGE_INSTALLED filename=$filename bytes=$expected_bytes sha256=$expected_sha"
