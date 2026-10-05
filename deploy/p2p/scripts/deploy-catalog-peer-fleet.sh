#!/usr/bin/env bash
set -euo pipefail

roster=${1:?roster file is required}
label=${2:-fleet}
root=${NVIS_P2P_ROOT:-/home/hlakshminara/NVIS-P2P}
parallelism=${NVIS_P2P_DEPLOY_PARALLELISM:-32}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
log=$root/state/catalog-peer-deploy-$label-$stamp.log

export NVIS_P2P_ROOT
set +e
xargs -r -P "$parallelism" -n 1 bash -c '
  worker=$1
  node=$2
  "$worker" "$node"
  rc=$?
  if (( rc != 0 )); then
    printf "DEPLOY_DEFERRED node=%s rc=%s\n" "$node" "$rc"
  fi
  exit 0
' _ "$root/scripts/deploy-catalog-peer-node.sh" < "$roster" | tee "$log"
pipeline_rc=${PIPESTATUS[0]}
set -e
ready=$(grep -c '^DEPLOY_READY ' "$log" || true)
deferred=$(grep -c '^DEPLOY_DEFERRED ' "$log" || true)
printf 'DEPLOY_COMPLETE label=%s requested=%s ready=%s deferred=%s worker_rc=%s log=%s\n' \
  "$label" "$(wc -l < "$roster")" "$ready" "$deferred" "$pipeline_rc" "$log"
(( ready + deferred == $(wc -l < "$roster") ))
