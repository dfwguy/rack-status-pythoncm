#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/good/release/bin"; printf x >"$tmp/good/release/bin/file"
tar -C "$tmp/good" -czf "$tmp/good.tar.gz" release
python3 "$ROOT/scripts/safe_extract.py" "$tmp/good.tar.gz" "$tmp/out" >/dev/null
[[ $(<"$tmp/out/release/bin/file") == x ]]
ln -s ../../escape "$tmp/good/release/bad"
tar -C "$tmp/good" -czf "$tmp/bad.tar.gz" release
if python3 "$ROOT/scripts/safe_extract.py" "$tmp/bad.tar.gz" "$tmp/badout" >/dev/null 2>&1; then
  echo 'expected symlink archive to be rejected' >&2; exit 1
fi
printf 'PASS safe archive extraction checks\n'
