#!/usr/bin/env bash
set -euo pipefail

base=${NVIS_P2P_BASE:-/opt/containers/.nvis-p2p}
catalog=$base/catalog/images.tsv
target_dir=${NVIS_P2P_CONTAINER_DIR:-/opt/containers}
bucket=${NVIS_P2P_BUCKET:-nvis-p2p-artifacts}
endpoint=${NVIS_P2P_DFSTORE_ENDPOINT:-$base/run/dfdaemon.sock}

[[ -r $catalog ]] || { echo "NVIS_P2P_SYNC_NO_CATALOG path=$catalog" >&2; exit 66; }
[[ -x $base/bin/dfstore ]] || { echo "NVIS_P2P_SYNC_NO_DFSTORE path=$base/bin/dfstore" >&2; exit 69; }
[[ -S $endpoint ]] || { echo "NVIS_P2P_SYNC_NO_PEER endpoint=$endpoint" >&2; exit 69; }
: "${DFSTORE_EXPORT_STORAGE_ENDPOINT:?}"
: "${DFSTORE_EXPORT_STORAGE_ACCESS_KEY_ID:?}"
: "${DFSTORE_EXPORT_STORAGE_ACCESS_KEY_SECRET:?}"

mkdir -p "$base/receipts" "$base/staging" "$base/quarantine" "$base/run" "$target_dir"
exec 9>"$base/run/catalog-sync.lock"
flock -n 9 || { echo "NVIS_P2P_SYNC_ALREADY_RUNNING"; exit 0; }

ready=0 installed=0 failed=0
while IFS=$'\t' read -r order display_name filename expected_bytes expected_sha; do
  [[ $order == order ]] && continue
  [[ $filename =~ ^[A-Za-z0-9+._-]+[.]sqsh$ ]] || { echo "NVIS_P2P_SYNC_BAD_FILENAME filename=$filename" >&2; failed=$((failed + 1)); continue; }
  target=$target_dir/$filename
  receipt=$base/receipts/$filename.sha256
  partial=$base/staging/$filename.dfstore.partial

  if [[ -f $target && $(stat -c %s "$target") == "$expected_bytes" ]]; then
    if [[ -s $receipt ]] && grep -Fxq "$expected_sha  $target" "$receipt"; then
      echo "NVIS_P2P_SYNC_READY filename=$filename verification=receipt"
      ready=$((ready + 1))
      continue
    fi
    actual_sha=$(sha256sum "$target" | awk '{print $1}')
    if [[ $actual_sha == "$expected_sha" ]]; then
      printf '%s  %s\n' "$expected_sha" "$target" > "$receipt"
      echo "NVIS_P2P_SYNC_READY filename=$filename verification=sha256"
      ready=$((ready + 1))
      continue
    fi
  fi

  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  [[ ! -e $partial ]] || mv "$partial" "$base/quarantine/$filename.partial.$stamp"
  if ! "$base/bin/dfstore" export \
      --endpoint "$endpoint" \
      --digest "sha256:$expected_sha" --timeout 8h --no-progress --console \
      --force-hard-link -O "$partial" \
      "s3://$bucket/images/$expected_sha/$filename"; then
    echo "NVIS_P2P_SYNC_EXPORT_FAILED filename=$filename" >&2
    failed=$((failed + 1))
    continue
  fi

  if [[ $(stat -c %s "$partial") != "$expected_bytes" ]]; then
    echo "NVIS_P2P_SYNC_SIZE_FAILED filename=$filename" >&2
    failed=$((failed + 1))
    continue
  fi
  actual_sha=$(sha256sum "$partial" | awk '{print $1}')
  if [[ $actual_sha != "$expected_sha" ]]; then
    echo "NVIS_P2P_SYNC_DIGEST_FAILED filename=$filename" >&2
    failed=$((failed + 1))
    continue
  fi
  [[ ! -e $target ]] || mv "$target" "$base/quarantine/$filename.pre-dfstore.$stamp"
  mv "$partial" "$target"
  printf '%s  %s\n' "$expected_sha" "$target" > "$receipt"
  echo "NVIS_P2P_SYNC_INSTALLED filename=$filename bytes=$expected_bytes sha256=$expected_sha"
  installed=$((installed + 1))
done < "$catalog"

total=$((ready + installed + failed))
echo "NVIS_P2P_SYNC_COMPLETE total=$total ready=$ready installed=$installed failed=$failed"
(( failed == 0 ))
