#!/usr/bin/env bash
set -euo pipefail

filename=${1:?canonical image filename is required}
roster=${2:?roster file is required}
label=${3:-fleet}
root=${NVIS_P2P_ROOT:-/home/hlakshminara/NVIS-P2P}
parallelism=${NVIS_P2P_DISTRIBUTE_PARALLELISM:-64}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
log=$root/state/catalog-distribute-$label-$filename-$stamp.log

row=$(awk -F '\t' -v image="$filename" '$3 == image {print; exit}' "$root/catalog/images.tsv")
[[ -n $row ]] || { echo "DISTRIBUTE_UNKNOWN filename=$filename" >&2; exit 64; }
IFS=$'\t' read -r order display_name filename expected_bytes expected_sha <<< "$row"

"$root/scripts/preheat-catalog-image.sh" "$filename" | tee -a "$log"
export NVIS_P2P_ROOT
xargs -r -P "$parallelism" -I{} "$root/scripts/launch-catalog-image-node.sh" "{}" "$filename" < "$roster" | tee -a "$log"

echo "DISTRIBUTE_LAUNCHED label=$label filename=$filename nodes=$(wc -l < "$roster") log=$log"
