#!/usr/bin/env bash
set -euo pipefail

roster=${1:?roster file is required}
label=${2:-fleet}
root=${NVIS_P2P_ROOT:-/home/hlakshminara/NVIS-P2P}
parallelism=${NVIS_P2P_PREFLIGHT_PARALLELISM:-64}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
output=$root/state/catalog-preflight-$label-$stamp.tsv
temporary=$(mktemp "$root/state/.catalog-preflight.XXXXXX")

printf 'node\tssh\tip\tpage_size\tarch\tfree_bytes\tuid\tgid\tsudo_n\topt_writable\tpeer\tdfdaemon_sha\tactive_dfget:ready_images:missing_bytes\n' > "$temporary"
xargs -r -P "$parallelism" -n 1 "$root/scripts/preflight-catalog-node.sh" < "$roster" >> "$temporary"
mv "$temporary" "$output"
printf 'PREFLIGHT_COMPLETE label=%s nodes=%s reachable=%s output=%s\n' \
  "$label" \
  "$(tail -n +2 "$output" | wc -l)" \
  "$(awk -F '\t' '$2 == "reachable" {n++} END {print n+0}' "$output")" \
  "$output"
