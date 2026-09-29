#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=/dev/null
source "$root/release/components.env"
destination=${1:-$root/release/stage/NVIS-P2P-$NVIS_P2P_RELEASE}

[[ ! -e $destination ]] || { echo "release destination already exists: $destination" >&2; exit 73; }
"$root/scripts/verify-release-inputs.sh"
mkdir -p "$destination"

while IFS= read -r path; do
  [[ -z $path || $path == \#* ]] && continue
  mkdir -p "$destination/$(dirname "$path")"
  cp -a "$root/$path" "$destination/$path"
done < <("$root/scripts/list-release-inputs.sh")

# Normalize mtimes so a later archive step can be byte-reproducible.
find "$destination" -print0 | xargs -0 touch --no-dereference --date="@$DRAGONFLY_SERVER_SOURCE_DATE_EPOCH"
echo "release tree prepared (no archive created): $destination"
