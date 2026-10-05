#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=/dev/null
source "$root/release/components.env"
output=$root/release/console-files.list
temporary=$(mktemp)
trap 'rm -f -- "$temporary"' EXIT

{
  find "$root/services/console-api" -type f -not -path '*/__pycache__/*'
  find "$root/deploy/console" -type f -not -path '*/artifacts/*'
  printf '%s\n' \
    "$root/deploy/console/artifacts/NVIS-P2P-Console-${NVIS_P2P_CONSOLE_RELEASE}-static.tar.gz" \
    "$root/deploy/console/artifacts/NVIS-P2P-Console-${NVIS_P2P_CONSOLE_RELEASE}-static.tar.gz.sha256"
  find "$root/upstream/console" -type f \
    -not -path '*/.git/*' \
    -not -path '*/node_modules/*' \
    -not -path '*/build/*' \
    -not -path '*/cypress/*' \
    -not -path '*/.github/*' \
    -not -path '*/docs/*'
  find "$root/docs" -type f
  find "$root/release/nvis-p2p-deploy" -type f
} | sed "s#^$root/##" | LC_ALL=C sort -u > "$temporary"

[[ -s $temporary ]] || { echo "console release list is empty" >&2; exit 66; }
mv -f -- "$temporary" "$output"
trap - EXIT
echo "console release list refreshed: $(wc -l < "$output") files"
