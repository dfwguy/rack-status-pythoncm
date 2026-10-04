#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# Prove the deployment works, not merely that it started — unauthenticated, because SRP identity
# needs no shared credential. Reuses the repository's own bin/verify-deploy.sh (the gate that
# encodes every past outage: black-screen assets, missing module bind mounts, stale modules,
# crash-loop "Restarting"), extracted from the image so the check matches the artifact.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$SCRIPT_DIR/lib.sh"
# shellcheck disable=SC2034  # read by nvis_die() in lib.sh
NVIS_PHASE=verify
nvis_load_env

for var in NVIS_CONTAINER NVIS_VERIFY_URL NVIS_DEPLOY_HOST NVIS_IMAGE_REF; do nvis_need_var "$var"; done
grep -Fqx "$NVIS_DEPLOY_HOST" < <(printf '%s\n%s\n%s\n' "$(hostname)" "$(hostname -s)" "$(hostname -f 2>/dev/null || true)") \
  || nvis_die 'verification is running on a host other than NVIS_DEPLOY_HOST'

container_id=$(docker inspect -f '{{.Id}}' "$NVIS_CONTAINER" 2>/dev/null) || nvis_die 'deployed container absent'
state=$(docker inspect -f '{{.State.Status}}' "$NVIS_CONTAINER")
[[ "$state" == running ]] || nvis_die "container is not running (state=$state)"
image_id=$(docker inspect -f '{{.Image}}' "$NVIS_CONTAINER")
image_ref=$(docker inspect -f '{{.Config.Image}}' "$NVIS_CONTAINER")
[[ "$image_ref" == "$NVIS_IMAGE_REF" ]] || nvis_die "running image ref $image_ref is not the deployed $NVIS_IMAGE_REF"

# The build identity the image carries, so an operator can attest exactly what is live.
build_id=$(docker exec "$NVIS_CONTAINER" sh -c 'cat /app/backend/build-info.json 2>/dev/null' | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("build","unknown"))
except Exception: print("unknown")' 2>/dev/null || echo unknown)

# Run the repository's own deployment gate against the live container. Copy it out of the image so
# it is the version that shipped, and run it locally (NVIS_DEPLOY_SSH empty).
vtmp=$(mktemp -d); trap 'rm -rf "$vtmp"' EXIT
vcid=$(docker create "$NVIS_IMAGE_REF")
docker cp "$vcid:/app/bin/verify-deploy.sh" "$vtmp/verify-deploy.sh" 2>/dev/null || { docker rm -f "$vcid" >/dev/null 2>&1 || true; nvis_die 'image lacks bin/verify-deploy.sh'; }
docker rm -f "$vcid" >/dev/null 2>&1 || true
chmod +x "$vtmp/verify-deploy.sh"
NVIS_DEPLOY_SSH='' NVIS_CONTAINER="$NVIS_CONTAINER" NVIS_VERIFY_PORT="${NVIS_WEB_PORT:-}" \
  bash "$vtmp/verify-deploy.sh" || nvis_die 'repository deployment gate failed'

# Independent unauthenticated liveness, through the published URL the operator actually uses.
nvis_health_ping || nvis_die "unauthenticated /api/status failed at $NVIS_VERIFY_URL"

printf 'VERIFY_OK host=%s container=%s container_id=%s image_ref=%s image_id=%s build_id=%s url=%s\n' \
  "$NVIS_DEPLOY_HOST" "$NVIS_CONTAINER" "$container_id" "$image_ref" "$image_id" "$build_id" "$NVIS_VERIFY_URL"
