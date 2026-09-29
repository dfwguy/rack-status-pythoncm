#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=/dev/null
source "$root/release/components.env"
mkdir -p "$root/downloads"

fetch_verified() {
  local url=$1 expected=$2 destination=$3 temporary
  if [[ -r $destination ]] && printf '%s  %s\n' "$expected" "$destination" | sha256sum -c - >/dev/null; then
    echo "verified cached artifact: $destination"
    return
  fi
  temporary=$(mktemp "${destination}.partial.XXXXXX")
  trap 'rm -f -- "$temporary"' RETURN
  curl --fail --location --retry 3 --output "$temporary" "$url"
  printf '%s  %s\n' "$expected" "$temporary" | sha256sum -c -
  mv -f -- "$temporary" "$destination"
  trap - RETURN
  echo "fetched verified artifact: $destination"
}

fetch_verified "$DRAGONFLY_SERVER_ARCHIVE_URL" "$DRAGONFLY_SERVER_ARCHIVE_SHA256" \
  "$root/downloads/dragonfly-${DRAGONFLY_SERVER_VERSION}-linux-amd64.tar.gz"
fetch_verified "$DRAGONFLY_CLIENT_AMD64_ARCHIVE_URL" "$DRAGONFLY_CLIENT_AMD64_ARCHIVE_SHA256" \
  "$root/downloads/dragonfly-client-v${DRAGONFLY_CLIENT_VERSION}-x86_64-unknown-linux-gnu.tar.gz"
