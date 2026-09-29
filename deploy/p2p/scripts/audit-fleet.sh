#!/usr/bin/env bash
set -euo pipefail

root=${NVIS_P2P_ROOT:-/home/hlakshminara/NVIS-P2P}
inventory=${1:-$root/inventory/SU1-288.nodes}
output=${2:-$root/state/SU1-peer-audit.tsv}
parallelism=${NVIS_P2P_AUDIT_PARALLELISM:-24}

{
  printf 'node\tstate\tdfdaemon_sha256\tdfget_sha256\trss_bytes\tthreads\ttarget_size\n'
  xargs -r -P "$parallelism" -n 1 "$root/scripts/audit-node.sh" < "$inventory"
} > "$output"

awk -F '\t' 'NR > 1 {count[$2]++} END {for (state in count) print state, count[state]}' "$output" | sort
