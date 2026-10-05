#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=/dev/null
source "$root/release/components.env"

fetch_one() {
  local name=$1 url=$2 commit=$3 destination=$4
  if [[ -e $destination ]]; then
    [[ -e $destination/.git ]] || { echo "$destination exists but is not a git checkout" >&2; exit 65; }
    [[ $(git -C "$destination" rev-parse HEAD) == "$commit" ]] || {
      echo "$name checkout is not at pinned commit: $destination" >&2; exit 65;
    }
    git -C "$destination" diff --quiet
    git -C "$destination" diff --cached --quiet
    echo "$name source already pinned: $commit"
    return
  fi
  mkdir -p "$(dirname "$destination")"
  git clone --filter=blob:none --no-checkout "$url" "$destination"
  git -C "$destination" fetch --depth=1 origin "$commit"
  git -C "$destination" checkout --detach "$commit"
  [[ $(git -C "$destination" rev-parse HEAD) == "$commit" ]]
  echo "$name source fetched: $commit"
}

fetch_one server "$DRAGONFLY_SERVER_SOURCE_URL" "$DRAGONFLY_SERVER_COMMIT" "$root/upstream/dragonfly"
fetch_one client "$DRAGONFLY_CLIENT_SOURCE_URL" "$DRAGONFLY_CLIENT_COMMIT" "$root/upstream/client-v1.4.9"
