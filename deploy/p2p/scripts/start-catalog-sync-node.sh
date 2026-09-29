#!/usr/bin/env bash
set -u

node=${1:?node is required}
wave=${2:?sync wave identifier is required}
service=nvis-p2p-catalog-sync.service
log=/opt/containers/.nvis-p2p/logs/catalog-sync.log
ssh_opts=(-o BatchMode=yes -o ConnectTimeout="${NVIS_P2P_SSH_TIMEOUT:-8}" \
  -o ServerAliveInterval=4 -o ServerAliveCountMax=1)

if ! result=$(ssh "${ssh_opts[@]}" "$node" bash -s -- "$service" "$log" "$wave" <<'REMOTE'
set -u
service=$1 log=$2 wave=$3
if ! sudo -n systemctl cat "$service" >/dev/null 2>&1; then
  printf 'START_FAILED\tservice_not_installed'
  exit 0
fi
mkdir -p "${log%/*}"
printf 'NVIS_P2P_SYNC_WAVE id=%s requested=%s\n' "$wave" "$(date -u +%FT%TZ)" >> "$log" || {
  printf 'START_FAILED\tlog_not_writable'
  exit 0
}
state=$(sudo -n systemctl is-active "$service" 2>/dev/null || true)
if [[ $state == activating ]]; then
  # Join a sync which is already running. Its completion line will follow our marker.
  sudo -n systemctl --no-block start "$service" >/dev/null 2>&1 || {
    printf 'START_FAILED\tsystemctl_start_failed'
    exit 0
  }
  printf 'STARTED\tjoined_active_sync'
else
  sudo -n systemctl reset-failed "$service" >/dev/null 2>&1 || true
  sudo -n systemctl --no-block restart "$service" >/dev/null 2>&1 || {
    printf 'START_FAILED\tsystemctl_restart_failed'
    exit 0
  }
  printf 'STARTED\trestart_queued'
fi
REMOTE
); then
  printf '%s\tUNREACHABLE\tssh_failed\n' "$node"
  exit 0
fi

printf '%s\t%s\n' "$node" "$result"
