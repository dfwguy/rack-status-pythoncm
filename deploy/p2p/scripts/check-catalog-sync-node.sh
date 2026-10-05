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
active=$(sudo -n systemctl is-active "$service" 2>/dev/null || true)
sub=$(sudo -n systemctl show -p SubState --value "$service" 2>/dev/null || true)
marker="NVIS_P2P_SYNC_WAVE id=$wave "
completion=''
detail=''
if [[ -r $log ]]; then
  completion=$(awk -v marker="$marker" '
    index($0, marker)==1 {seen=1; next}
    seen && /^NVIS_P2P_SYNC_COMPLETE / {last=$0}
    END {print last}' "$log")
  detail=$(tail -n 1 "$log" 2>/dev/null | tr '\t\r\n' '   ' | cut -c1-240)
fi

if [[ $completion =~ failed=0($|[[:space:]]) ]]; then
  printf 'COMPLETE\t%s\t%s/%s' "$completion" "${active:-unknown}" "${sub:-unknown}"
elif [[ -n $completion ]]; then
  printf 'FAILED\t%s\t%s/%s' "$completion" "${active:-unknown}" "${sub:-unknown}"
elif [[ $active == failed || $sub == failed ]]; then
  printf 'FAILED\t%s\t%s/%s' "${detail:-service_failed_without_completion}" "${active:-unknown}" "${sub:-unknown}"
elif [[ $active == inactive || $sub == dead ]]; then
  printf 'FAILED\tmarker_missing\t%s/%s' "${active:-unknown}" "${sub:-unknown}"
elif [[ ! -r $log ]]; then
  printf 'FAILED\tcatalog_sync_log_missing\t%s/%s' "${active:-unknown}" "${sub:-unknown}"
else
  printf 'PENDING\t%s\t%s/%s' "${detail:-waiting_for_sync_output}" "${active:-unknown}" "${sub:-unknown}"
fi
REMOTE
); then
  printf '%s\tUNREACHABLE\tssh_failed\tunknown\n' "$node"
  exit 0
fi

printf '%s\t%s\n' "$node" "$result"
