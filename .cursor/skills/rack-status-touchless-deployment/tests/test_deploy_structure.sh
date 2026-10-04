#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# Structural checks: every script parses, the state machine has the documented phase order, and the
# mutation boundary and rollback wiring are present. No cluster, no Docker.
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SCRIPTS="$HERE/../scripts"
fail=0
check() { if eval "$1"; then printf 'ok   %s\n' "$2"; else printf 'FAIL %s\n' "$2"; fail=1; fi; }

# 1. Every shell script parses.
for s in "$SCRIPTS"/*.sh; do
  if bash -n "$s"; then printf 'ok   bash -n %s\n' "$(basename "$s")"; else printf 'FAIL bash -n %s\n' "$(basename "$s")"; fail=1; fi
done

# 2. The orchestrator runs the phases in the documented order.
order=$(grep -oE 'run_phase [a-z_]+' "$SCRIPTS/deploy.sh" | awk '{print $2}' | paste -sd, -)
check '[[ "$order" == "preflight,load,drift,snapshot,scripts,deploy,verify,activate,smoke" ]]' "phase order matches OPERATIONS.md"

# 3. The mutation boundary and automatic rollback are wired.
check 'grep -q "mutation_started=1" "$SCRIPTS/deploy.sh"' "deploy marks the mutation boundary"
check 'grep -q "rollback.sh" "$SCRIPTS/deploy.sh"' "deploy invokes rollback on error"
check 'grep -q "trap on_error ERR" "$SCRIPTS/deploy.sh"' "deploy traps errors"

# 4. No sudo, no host-key bypass, no auth-disable anywhere in the skill scripts.
check '! grep -RnE "sudo docker|StrictHostKeyChecking=no|RACK_WEB_AUTH_TOKEN|AUTH_DISABLED" "$SCRIPTS"' "no sudo/host-key-bypass/web-token/auth-disable"

# 5. Verification is unauthenticated (no cookie login carried over from the source-deploy skill).
check '! grep -q "nvis_cookie_login" "$SCRIPTS/lib.sh"' "no web-cookie login helper"
check 'grep -q "verify-deploy.sh" "$SCRIPTS/verify.sh"' "verify reuses the repository gate"

exit "$fail"
