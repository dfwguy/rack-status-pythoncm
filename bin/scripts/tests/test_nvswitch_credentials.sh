#!/usr/bin/env bash

set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=bin/scripts/lib/nvswitch_credentials.sh
source "${HERE}/../lib/nvswitch_credentials.sh"

test_dir=$(mktemp -d)
trap 'rm -rf "$test_dir"' EXIT
chmod 0700 "$test_dir"

printf '%s\n' 'fixture-value' >"$test_dir/good"
chmod 0600 "$test_dir/good"
nvis_secret_file TEST_CREDENTIAL_FILE "$test_dir/good"
[[ "$REPLY" == "$test_dir/good" ]]

chmod 0644 "$test_dir/good"
if nvis_secret_file TEST_CREDENTIAL_FILE "$test_dir/good" 2>/dev/null; then
	echo "world-readable credential was accepted" >&2
	exit 1
fi
chmod 0600 "$test_dir/good"

printf '\n' >"$test_dir/empty"
chmod 0600 "$test_dir/empty"
if nvis_secret_file TEST_CREDENTIAL_FILE "$test_dir/empty" 2>/dev/null; then
	echo "empty credential was accepted" >&2
	exit 1
fi

printf '%s\n%s' 'first-line' 'second-line' >"$test_dir/multiline"
chmod 0600 "$test_dir/multiline"
if nvis_secret_file TEST_CREDENTIAL_FILE "$test_dir/multiline" 2>/dev/null; then
	echo "multi-line credential was accepted" >&2
	exit 1
fi

ln -s "$test_dir/good" "$test_dir/link"
if nvis_secret_file TEST_CREDENTIAL_FILE "$test_dir/link" 2>/dev/null; then
	echo "credential symlink was accepted" >&2
	exit 1
fi

nvis_make_netrc example.invalid operator "$test_dir/good" "$test_dir/netrc"
[[ $(stat -c '%a' "$test_dir/netrc") == 600 ]]
grep -q '^machine example.invalid login "operator" password "' "$test_dir/netrc"

nvis_make_password_json "$test_dir/good" "$test_dir/payload.json"
[[ $(stat -c '%a' "$test_dir/payload.json") == 600 ]]
python3 -c 'import json,sys; assert json.load(open(sys.argv[1], encoding="utf-8"))["Password"] == "fixture-value"' "$test_dir/payload.json"

echo "nvswitch credential tests passed"
