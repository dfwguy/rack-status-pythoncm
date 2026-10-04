#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# Unit tests for lib.sh's sealed-environment parser. No cluster, no Docker.
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LIB="$HERE/../scripts/lib.sh"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
fail=0
check() { if [[ "$1" == "$2" ]]; then printf 'ok   %s\n' "$3"; else printf 'FAIL %s (got %q want %q)\n' "$3" "$1" "$2"; fail=1; fi; }

# Each case runs the parser in a subshell so a successful parse does not leak into the next.
run_parse() { # <env-file>  -> prints "RC|VALUE_OF_NVIS_CONTAINER"
  ( set -e; source "$LIB"; NVIS_DEPLOY_ENV="$1" nvis_load_env 2>/dev/null; printf '%s' "${NVIS_CONTAINER:-}" ) 2>/dev/null
  printf '|%s' "$?"
}

# 1. A valid file parses, strips quotes, and exports an allowlisted key.
f="$tmp/good.env"; printf 'NVIS_CONTAINER="nvis-rack-status-nc"\n# a comment\nNVIS_WEB_PORT=5056\n' >"$f"; chmod 600 "$f"
out=$(run_parse "$f"); check "${out%|*}" "nvis-rack-status-nc" "valid file parses and strips quotes"
check "${out#*|}" "0" "valid file returns success"

# 2. A key not on the allowlist is rejected.
f="$tmp/badkey.env"; printf 'RACK_WEB_AUTH_TOKEN_FILE=/x\n' >"$f"; chmod 600 "$f"
out=$(run_parse "$f"); check "${out#*|}" "2" "non-allowlisted key is rejected"

# 3. Wrong permissions are rejected.
f="$tmp/perm.env"; printf 'NVIS_CONTAINER=x\n' >"$f"; chmod 644 "$f"
out=$(run_parse "$f"); check "${out#*|}" "2" "world-readable file is rejected"

# 4. Shell-ish syntax (interpolation / command substitution) is not evaluated — treated as a value.
f="$tmp/inject.env"; printf 'NVIS_CONTAINER=$(touch %s/pwned)\n' "$tmp" >"$f"; chmod 600 "$f"
run_parse "$f" >/dev/null
check "$( [[ -e "$tmp/pwned" ]] && echo yes || echo no )" "no" "command substitution is not evaluated"

# 5. A lowercase / malformed assignment is rejected.
f="$tmp/syntax.env"; printf 'not a valid line\n' >"$f"; chmod 600 "$f"
out=$(run_parse "$f"); check "${out#*|}" "2" "malformed line is rejected"

exit "$fail"
