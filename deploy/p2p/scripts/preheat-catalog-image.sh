#!/usr/bin/env bash
set -euo pipefail

filename=${1:?canonical image filename is required}
root=${NVIS_P2P_ROOT:-/home/hlakshminara/NVIS-P2P}
catalog=$root/catalog/images.tsv
client=$root/dist/client-amd64-v1.4.9/dfget
origin=http://10.41.112.3:12130
output_root=/cluster/OVA/NVIS-P2P/preheat/catalog-v1

row=$(awk -F '\t' -v image="$filename" '$3 == image {print; exit}' "$catalog")
[[ -n $row ]] || { echo "PREHEAT_UNKNOWN filename=$filename" >&2; exit 64; }
IFS=$'\t' read -r order display_name filename expected_bytes expected_sha <<< "$row"

for seed in seed seed2; do
  endpoint=$root/state/$seed/dfdaemon.sock
  output=$output_root/$seed/$filename
  receipt=$output_root/$seed/$filename.sha256
  mkdir -p "$output_root/$seed"
  if [[ -f $output && $(stat -c %s "$output") == "$expected_bytes" && -s $receipt ]] &&
      grep -Fxq "$expected_sha  $output" "$receipt"; then
    echo "PREHEAT_READY seed=$seed filename=$filename verification=receipt"
    continue
  fi
  partial=$output.partial.$(date -u +%Y%m%dT%H%M%SZ)
  ionice -c2 -n4 nice -n 5 "$client" \
    --endpoint "$endpoint" \
    --no-progress \
    --force-hard-link \
    --timeout 6h \
    --priority 5 \
    --digest "sha256:$expected_sha" \
    --content-for-calculating-task-id "nvis-p2p-sha256:$expected_sha" \
    --application NVIS-P2P-Catalog \
    --tag "catalog-v1-$order" \
    "$origin/$filename" \
    -O "$partial" \
    --console
  [[ $(stat -c %s "$partial") == "$expected_bytes" ]]
  [[ $(sha256sum "$partial" | awk '{print $1}') == "$expected_sha" ]]
  if [[ -e $output ]]; then
    mv "$output" "$output.pre-catalog-v1.$(date -u +%Y%m%dT%H%M%SZ)"
  fi
  mv "$partial" "$output"
  printf '%s  %s\n' "$expected_sha" "$output" > "$receipt"
  echo "PREHEAT_READY seed=$seed filename=$filename verification=sha256"
done
