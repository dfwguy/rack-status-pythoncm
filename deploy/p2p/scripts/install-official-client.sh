#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=/dev/null
source "$root/release/components.env"

archive=${1:-$root/downloads/dragonfly-client-v${DRAGONFLY_CLIENT_VERSION}-x86_64-unknown-linux-gnu.tar.gz}
output=$root/dist/client-amd64-v${DRAGONFLY_CLIENT_VERSION}

[[ -r $archive ]] || { echo "missing upstream client archive: $archive" >&2; exit 66; }
printf '%s  %s\n' "$DRAGONFLY_CLIENT_AMD64_ARCHIVE_SHA256" "$archive" | sha256sum -c -
temporary=$(mktemp -d)
trap 'rm -rf -- "$temporary"' EXIT
tar -xzf "$archive" -C "$temporary"
source_root=$temporary/dragonfly-client-v${DRAGONFLY_CLIENT_VERSION}-x86_64-unknown-linux-gnu
mkdir -p "$output"
for binary in dfdaemon dfget dfstore; do
  [[ -x $source_root/$binary ]] || { echo "archive missing executable: $binary" >&2; exit 65; }
  install -m 0755 "$source_root/$binary" "$output/$binary"
done
echo "official amd64 client installed: version=$DRAGONFLY_CLIENT_VERSION output=$output"
