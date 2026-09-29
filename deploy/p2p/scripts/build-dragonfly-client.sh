#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=/dev/null
source "$root/release/components.env"

usage() {
  echo "usage: $0 <amd64|arm64-64k|all> [source-directory]" >&2
  exit 64
}

selection=${1:-}
source_dir=${2:-$root/upstream/client-v1.4.9}
case "$selection" in amd64|arm64-64k|all) ;; *) usage ;; esac

[[ -e $source_dir/.git ]] || { echo "client source is not a git checkout: $source_dir" >&2; exit 66; }
actual_commit=$(git -C "$source_dir" rev-parse HEAD)
[[ $actual_commit == "$DRAGONFLY_CLIENT_COMMIT" ]] || {
  echo "client source commit mismatch: expected=$DRAGONFLY_CLIENT_COMMIT actual=$actual_commit" >&2
  exit 65
}
git -C "$source_dir" diff --quiet
git -C "$source_dir" diff --cached --quiet

patch_file=$root/build/patches/client-v1.4.9-source-date-epoch.patch
[[ -r $patch_file ]] || { echo "missing reproducibility patch: $patch_file" >&2; exit 66; }

build_one() {
  local flavor=$1 target dockerfile output image strip_tool lg_page
  case "$flavor" in
    amd64)
      target=x86_64-unknown-linux-gnu
      dockerfile=$root/build/Dockerfile.amd64-client
      output=$root/dist/client-amd64-v1.4.9
      image=nvis-p2p-builder:client-v1.4.9-amd64
      strip_tool=strip
      lg_page=
      ;;
    arm64-64k)
      target=aarch64-unknown-linux-gnu
      dockerfile=$root/build/Dockerfile.arm64-client
      output=$root/dist/client-arm64-64k-v1.4.9
      image=nvis-p2p-builder:client-v1.4.9-arm64-64k
      strip_tool=aarch64-linux-gnu-strip
      lg_page=16
      ;;
  esac

  docker build --pull=false --file "$dockerfile" --tag "$image" "$root"
  mkdir -p "$output"
  local -a docker_env
  docker_env=(
    --env HOME=/tmp
    --env CARGO_HOME=/tmp/cargo
    --env SOURCE_DATE_EPOCH="$DRAGONFLY_CLIENT_SOURCE_DATE_EPOCH"
    --env GIT_COMMIT_HASH="$DRAGONFLY_CLIENT_COMMIT"
    --env GIT_COMMIT_SHORT_HASH="${DRAGONFLY_CLIENT_COMMIT:0:9}"
    --env GIT_COMMIT_DATE=2026-08-12
    --env CARGO_BUILD_JOBS="${NVIS_BUILD_JOBS:-16}"
  )
  # jemalloc's page-size override is required only for the NVIDIA 64K-page
  # ARM64 build. Exporting it with an empty value produces invalid C macros.
  if [[ -n $lg_page ]]; then
    docker_env+=(--env JEMALLOC_SYS_WITH_LG_PAGE="$lg_page")
  fi

  docker run --rm \
    --user "$(id -u):$(id -g)" \
    "${docker_env[@]}" \
    --volume "$source_dir:/source:ro" \
    --volume "$patch_file:/reproducible.patch:ro" \
    --volume "$output:/output" \
    "$image" bash -euo pipefail -c '
      mkdir -p /tmp/cargo /tmp/work
      tar -C /source --exclude=target -cf - . | tar -C /tmp/work -xf -
      cd /tmp/work
      patch -p1 --forward < /reproducible.patch
      export RUSTFLAGS="-C link-arg=-Wl,--build-id=none"
      cargo build --locked --release --workspace --exclude hdfs \
        --target '"$target"' --bin dfdaemon --bin dfget --bin dfstore
      for binary in dfdaemon dfget dfstore; do
        '"$strip_tool"' "target/'"$target"'/release/$binary"
        install -m 0755 "target/'"$target"'/release/$binary" "/output/$binary"
      done
    '
}

case "$selection" in
  amd64|arm64-64k) build_one "$selection" ;;
  all) build_one amd64; build_one arm64-64k ;;
esac

echo "client build complete: version=$DRAGONFLY_CLIENT_VERSION commit=$DRAGONFLY_CLIENT_COMMIT selection=$selection"
