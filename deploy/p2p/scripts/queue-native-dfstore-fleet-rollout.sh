#!/usr/bin/env bash
set -euo pipefail

root=${NVIS_P2P_ROOT:-/home/hlakshminara/NVIS-P2P}
prior_run=${1:?prior catalog-sync run directory is required}
prior_pid=${2:?prior controller PID is required}
log=${3:?queue log is required}
lock=$root/state/native-dfstore-fleet-rollout.lock

exec 9>"$lock"
flock -n 9 || { echo "ROLLOUT_QUEUE_REJECTED reason=already_running"; exit 75; }
exec >>"$log" 2>&1

echo "ROLLOUT_QUEUE_BEGIN timestamp=$(date -u +%FT%TZ) prior_run=$prior_run"
while [[ ! -s $prior_run/summary.tsv ]]; do
  if ! kill -0 "$prior_pid" 2>/dev/null; then
    echo "ROLLOUT_QUEUE_FAILED reason=prior_controller_exited_without_summary timestamp=$(date -u +%FT%TZ)"
    exit 1
  fi
  sleep 60
done

for sus in 1,2,3 4,5,6 7,8,9 10,11; do
  if ! curl -fksS --max-time 10 https://10.41.112.3:19000/minio/health/ready >/dev/null; then
    echo "ROLLOUT_QUEUE_FAILED reason=object_store_unhealthy sus=$sus timestamp=$(date -u +%FT%TZ)"
    exit 1
  fi
  echo "ROLLOUT_WAVE_BEGIN sus=$sus timestamp=$(date -u +%FT%TZ)"
  NVIS_P2P_SYNC_START_PARALLELISM=12 \
  NVIS_P2P_SYNC_PARALLELISM=32 \
  NVIS_P2P_SYNC_MAX_START_ATTEMPTS=3 \
  NVIS_P2P_SYNC_START_RETRY_SECONDS=5 \
  NVIS_P2P_SYNC_POLL_SECONDS=60 \
  NVIS_P2P_SYNC_TIMEOUT_SECONDS=86400 \
    "$root/scripts/catalog-operation.sh" sync "$sus" ""
  echo "ROLLOUT_WAVE_END sus=$sus timestamp=$(date -u +%FT%TZ)"
done

echo "ROLLOUT_QUEUE_COMPLETE timestamp=$(date -u +%FT%TZ)"
