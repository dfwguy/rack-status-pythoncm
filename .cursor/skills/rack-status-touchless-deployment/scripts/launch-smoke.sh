#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# Optional, explicitly-approved validation smoke launch. Denied by default; authorized ONLY when
# the sealed environment carries NVIS_SMOKE_APPROVED=YES with an exact preset, scope, and node
# list. This script never invents targets and never launches without that approval.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/lib.sh"
# shellcheck disable=SC2034  # read by nvis_die() in lib.sh
NVIS_PHASE=smoke
nvis_load_env

[[ "${NVIS_SMOKE_APPROVED:-NO}" == YES ]] || { printf 'smoke=NOT_AUTHORIZED\n'; exit 0; }
for var in NVIS_SMOKE_PRESET NVIS_SMOKE_SCOPE NVIS_SMOKE_NODES NVIS_CONTAINER; do nvis_need_var "$var"; done
[[ "$NVIS_SMOKE_SCOPE" =~ ^(node|rack|su)$ ]] || nvis_die 'NVIS_SMOKE_SCOPE must be node, rack, or su'
[[ "$NVIS_SMOKE_NODES" =~ ^[A-Za-z0-9._-]+(,[A-Za-z0-9._-]+)*$ ]] || nvis_die 'smoke nodes must be an exact comma-separated list'

# Fail closed on ineligible targets: every named node must be known to the scheduler before any
# launch. This is a read-only check inside the just-deployed container, which already holds the
# cluster connection. A node the scheduler does not know is a hard stop, not a warning.
unknown=$(docker exec "$NVIS_CONTAINER" python3 - "$NVIS_SMOKE_NODES" <<'PY'
import sys, os
sys.path.insert(0, "/app/backend")
os.environ.setdefault("PYTHONPATH", "/app/backend")
wanted = [n for n in sys.argv[1].split(",") if n]
try:
    import bcm_query
    data = bcm_query.query("nodes").get("data", [])
    known = {d.get("hostname") for d in data}
    print(",".join(n for n in wanted if n not in known))
except Exception as exc:  # a broken query must not read as "all eligible"
    print("QUERY_FAILED:%s" % exc)
PY
) || nvis_die 'eligibility query failed'
[[ "$unknown" != QUERY_FAILED:* ]] || nvis_die "scheduler eligibility query failed: ${unknown#QUERY_FAILED:}"
[[ -z "$unknown" ]] || nvis_die "ineligible/unknown smoke nodes: $unknown"

nvis_record "smoke=ELIGIBLE preset=$NVIS_SMOKE_PRESET scope=$NVIS_SMOKE_SCOPE nodes=$NVIS_SMOKE_NODES"
# The actual validation launch is performed through the product's own validation flow with the
# approved preset and the eligibility-checked nodes. It is deliberately a separate, operator-driven
# action rather than an implicit side effect of a deployment; this phase confirms readiness and
# records the approved plan as evidence.
printf 'SMOKE_READY preset=%s scope=%s nodes=%s\n' "$NVIS_SMOKE_PRESET" "$NVIS_SMOKE_SCOPE" "$NVIS_SMOKE_NODES"
