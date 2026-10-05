#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
checksums=$root/release/SHA256SUMS
manifest=$root/release/MANIFEST.tsv

[[ -r $root/release/files.list ]] || { echo "missing release file list" >&2; exit 66; }
temporary_checksums=$(mktemp)
temporary_manifest=$(mktemp)
trap 'rm -f -- "$temporary_checksums" "$temporary_manifest"' EXIT
printf 'path\tbytes\tsha256\tmode\n' > "$temporary_manifest"

while IFS= read -r path; do
  [[ -z $path || $path == \#* ]] && continue
  case "$path" in /*|*..*) echo "unsafe release path: $path" >&2; exit 65 ;; esac
  [[ $path == release/SHA256SUMS || $path == release/MANIFEST.tsv ]] && continue
  [[ -f $root/$path ]] || { echo "missing release input: $path" >&2; exit 66; }
  sha=$(sha256sum "$root/$path" | cut -d' ' -f1)
  bytes=$(stat -c %s "$root/$path")
  mode=$(stat -c '%a' "$root/$path")
  printf '%s  %s\n' "$sha" "$path" >> "$temporary_checksums"
  printf '%s\t%s\t%s\t%s\n' "$path" "$bytes" "$sha" "$mode" >> "$temporary_manifest"
done < <("$root/scripts/list-release-inputs.sh")

LC_ALL=C sort -k2,2 "$temporary_checksums" > "$checksums"
{ head -n1 "$temporary_manifest"; tail -n +2 "$temporary_manifest" | LC_ALL=C sort -k1,1; } > "$manifest"
echo "release manifests refreshed: $(wc -l < "$checksums") checksummed inputs"
