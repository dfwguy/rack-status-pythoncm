#!/usr/bin/env bash
set -euo pipefail

node=${1:?node is required}
root=${NVIS_P2P_ROOT:-/home/hlakshminara/NVIS-P2P}
base=/opt/containers/.nvis-p2p
unit=$root/deploy/systemd/nvis-p2p-catalog-sync.service
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)

case "$node" in
  ink50-dh02-[a-k][0-9][0-9]-*|ink50-dh01-[a-c][0-9][0-9]-*) ;;
  *) echo "SYNC_UNIT_REJECTED node=$node reason=outside-SU1-SU14" >&2; exit 64 ;;
esac

scp -q "${ssh_opts[@]}" "$unit" "$node:$base/nvis-p2p-catalog-sync.service.next"
ssh "${ssh_opts[@]}" "$node" bash -s -- "$base" <<'REMOTE'
set -euo pipefail
base=$1
sudo -n install -m 0644 "$base/nvis-p2p-catalog-sync.service.next" /etc/systemd/system/nvis-p2p-catalog-sync.service
sudo -n systemctl daemon-reload
sudo -n systemctl enable nvis-p2p-catalog-sync.service >/dev/null
REMOTE
echo "SYNC_UNIT_READY node=$node"
