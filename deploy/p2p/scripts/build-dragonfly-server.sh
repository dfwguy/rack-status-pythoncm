#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=/dev/null
source "$root/release/components.env"

mode=${1:---from-archive}
output=$root/dist/core
mkdir -p "$output"

case "$mode" in
  --from-archive)
    archive=$root/downloads/dragonfly-${DRAGONFLY_SERVER_VERSION}-linux-amd64.tar.gz
    [[ -r $archive ]] || { echo "missing upstream server archive: $archive" >&2; exit 66; }
    printf '%s  %s\n' "$DRAGONFLY_SERVER_ARCHIVE_SHA256" "$archive" | sha256sum -c -
    temporary=$(mktemp -d)
    trap 'rm -rf -- "$temporary"' EXIT
    tar -xzf "$archive" -C "$temporary"
    for file in manager scheduler; do
      install -m 0755 "$temporary/$file" "$output/$file"
    done
    for file in CHANGELOG.md LICENSE README.md; do
      install -m 0644 "$temporary/$file" "$output/$file"
    done
    ;;
  --from-source)
    source_dir=${2:-$root/upstream/dragonfly}
    [[ -e $source_dir/.git ]] || { echo "server source is not a git checkout: $source_dir" >&2; exit 66; }
    actual_commit=$(git -C "$source_dir" rev-parse HEAD)
    [[ $actual_commit == "$DRAGONFLY_SERVER_COMMIT" ]] || {
      echo "server source commit mismatch: expected=$DRAGONFLY_SERVER_COMMIT actual=$actual_commit" >&2
      exit 65
    }
    git -C "$source_dir" diff --quiet
    git -C "$source_dir" diff --cached --quiet
    image=golang@${GO_BUILDER_AMD64_DIGEST}
    temporary=$(mktemp -d)
    trap 'rm -rf -- "$temporary"' EXIT
    docker run --rm \
      --user "$(id -u):$(id -g)" \
      --env HOME=/tmp \
      --env GOCACHE=/tmp/go-cache \
      --env GOMODCACHE=/tmp/go-mod \
      --volume "$source_dir:/source:ro" \
      --volume "$temporary:/output" \
      "$image" bash -euo pipefail -c '
        mkdir -p /tmp/work /tmp/go-cache /tmp/go-mod
        tar -C /source --exclude=.go --exclude=.cache --exclude=bin -cf - . | tar -C /tmp/work -xf -
        cd /tmp/work
        commit='"${DRAGONFLY_SERVER_COMMIT:0:8}"'
        build_time=$(date -u -d @'"$DRAGONFLY_SERVER_SOURCE_DATE_EPOCH"' +%Y-%m-%dT%H:%M:%SZ)
        ldflags="-s -w -buildid= -X d7y.io/dragonfly/v2/version.GitCommit=$commit -X d7y.io/dragonfly/v2/version.BuildTime=$build_time -X d7y.io/dragonfly/v2/version.Gotags=none -X d7y.io/dragonfly/v2/version.Gogcflags=none"
        for pair in manager:manager scheduler:scheduler; do
          binary=${pair%%:*}; package=${pair##*:}
          CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -mod=readonly -trimpath -buildvcs=false -ldflags "$ldflags" -o "/output/$binary" "./cmd/$package"
        done
      '
    install -m 0755 "$temporary/manager" "$output/manager"
    install -m 0755 "$temporary/scheduler" "$output/scheduler"
    install -m 0644 "$source_dir/CHANGELOG.md" "$source_dir/LICENSE" "$source_dir/README.md" "$output/"
    ;;
  *) echo "usage: $0 [--from-archive|--from-source [source-directory]]" >&2; exit 64 ;;
esac

echo "server build prepared: version=$DRAGONFLY_SERVER_VERSION commit=$DRAGONFLY_SERVER_COMMIT mode=$mode"
