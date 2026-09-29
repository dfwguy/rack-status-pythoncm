#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck source=/dev/null
source "$root/release/components.env"
cd "$root"

errors=0
fail() { echo "VERIFY_ERROR: $*" >&2; errors=$((errors + 1)); }

expanded_inputs=$(mktemp)
trap 'rm -f -- "$expanded_inputs"' EXIT
"$root/scripts/list-release-inputs.sh" > "$expanded_inputs"
duplicates=$(sort "$expanded_inputs" | uniq -d)
[[ -z $duplicates ]] || fail "duplicate release paths: $duplicates"
while IFS= read -r path; do
  [[ -z $path || $path == \#* ]] && continue
  case "$path" in /*|*..*) fail "unsafe release path: $path"; continue ;; esac
  [[ -f $path ]] || fail "missing release input: $path"
done < "$expanded_inputs"

sha256sum -c release/SHA256SUMS || fail "checksum manifest mismatch"

printf '%s  %s\n' \
  "$DRAGONFLY_SERVER_MANAGER_SHA256" dist/core/manager \
  "$DRAGONFLY_SERVER_SCHEDULER_SHA256" dist/core/scheduler \
  "$DRAGONFLY_CLIENT_AMD64_DFDAEMON_SHA256" dist/client-amd64-v1.4.9/dfdaemon \
  "$DRAGONFLY_CLIENT_AMD64_DFGET_SHA256" dist/client-amd64-v1.4.9/dfget \
  "$DRAGONFLY_CLIENT_AMD64_DFSTORE_SHA256" dist/client-amd64-v1.4.9/dfstore \
  "$DRAGONFLY_CLIENT_ARM64_64K_DFDAEMON_SHA256" dist/client-arm64-64k-v1.4.9/dfdaemon \
  "$DRAGONFLY_CLIENT_ARM64_64K_DFGET_SHA256" dist/client-arm64-64k-v1.4.9/dfget \
  "$DRAGONFLY_CLIENT_ARM64_64K_DFSTORE_SHA256" dist/client-arm64-64k-v1.4.9/dfstore \
  | sha256sum -c - || fail "pinned component checksum mismatch"

server_version=$(dist/core/manager version 2>/dev/null | awk -F '\t' '$1=="GitVersion:"{print $2}')
server_commit=$(dist/core/manager version 2>/dev/null | awk -F '\t' '$1=="GitCommit:"{print $2}')
[[ $server_version == v$DRAGONFLY_SERVER_VERSION ]] || fail "manager version is $server_version"
[[ $server_commit == ${DRAGONFLY_SERVER_COMMIT:0:8} ]] || fail "manager commit is $server_commit"
dist/core/scheduler version 2>/dev/null | grep -Fq "v$DRAGONFLY_SERVER_VERSION" || fail "scheduler version mismatch"

for binary in dfdaemon dfget dfstore; do
  path=dist/client-amd64-v1.4.9/$binary
  [[ -x $path ]] || { fail "missing executable $path"; continue; }
  "$path" --version 2>/dev/null | grep -Fq " $DRAGONFLY_CLIENT_VERSION " || fail "$path version mismatch"
  file "$path" | grep -Fq 'x86-64' || fail "$path architecture mismatch"

  arm_path=dist/client-arm64-64k-v1.4.9/$binary
  [[ -x $arm_path ]] || { fail "missing executable $arm_path"; continue; }
  file "$arm_path" | grep -Fq 'ARM aarch64' || fail "$arm_path architecture mismatch"
  load_alignments=$(readelf -lW "$arm_path" | awk '$1=="LOAD"{print $NF}' | sort -u)
  [[ $load_alignments == 0x10000 ]] || fail "$arm_path is not exclusively 64K LOAD-aligned: $load_alignments"
done

while IFS= read -r script; do bash -n "$script" || fail "shell syntax failed: $script"; done < <(sed -n '/^scripts\/.*[.]sh$/p' "$expanded_inputs")

if command -v systemd-analyze >/dev/null; then
  verify_output=$(systemd-analyze verify deploy/systemd/*.service 2>&1 || true)
  # These compute-node units intentionally reference files installed later by
  # the deployment script, so host-local existence warnings are not syntax
  # failures. Preserve every other diagnostic as a release failure.
  unexpected=$(printf '%s\n' "$verify_output" | grep -vE '^nvis-p2p-(catalog-sync|peer)[.]service: Command .* is not executable: No such file or directory$' || true)
  [[ -z $unexpected ]] || fail "systemd unit verification failed: $unexpected"
fi

release_paths=$(sed '/^scripts\/verify-release-inputs[.]sh$/d' "$expanded_inputs" | paste -sd' ' -)
if rg -n 'dragonfly-client-v1[.]4[.]2|client-arm64-64k-patched|client-arm64-64k/' $release_paths; then
  fail "release inputs retain legacy client references"
fi
if rg -n 'DFSTORE_EXPORT_STORAGE_ACCESS_KEY_SECRET=[^$]|password:[[:space:]]+nvis_|BEGIN (RSA |OPENSSH )?PRIVATE KEY' $release_paths; then
  fail "release inputs contain credential material"
fi

python3 - <<'PY' || fail "catalog JSON/TSV mismatch"
import csv, json
from pathlib import Path

rows = list(csv.DictReader(Path("catalog/images.tsv").open(), delimiter="\t"))
payload = json.loads(Path("catalog/images.json").read_text())
images = payload["images"] if isinstance(payload, dict) else payload
assert len(rows) == len(images)
for row, image in zip(rows, images):
    assert row["filename"] == image["filename"]
    assert int(row["bytes"]) == int(image["bytes"])
    assert row["sha256"] == image["sha256"]
PY

console_bundle="deploy/console/artifacts/NVIS-P2P-Console-${NVIS_P2P_CONSOLE_RELEASE}-static.tar.gz"
console_checksum="$console_bundle.sha256"
sha256sum -c "$console_checksum" || fail "console static bundle checksum mismatch"
CONSOLE_BUNDLE="$console_bundle" python3 - <<'PY' || fail "console bundle lacks required deep links"
import os, re, tarfile
with tarfile.open(os.environ["CONSOLE_BUNDLE"], "r:gz") as archive:
    names = [name for name in archive.getnames() if re.search(r"static/js/main[.].*[.]js$", name)]
    assert len(names) == 1
    payload = archive.extractfile(names[0]).read()
    assert b"/nvis/settings" in payload
    assert b"/nvis/artifacts" in payload
PY

(( errors == 0 )) || { echo "release verification failed: errors=$errors" >&2; exit 1; }
echo "release verification passed: release=$NVIS_P2P_RELEASE"
