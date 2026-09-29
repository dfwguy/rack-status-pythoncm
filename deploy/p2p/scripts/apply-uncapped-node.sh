#!/usr/bin/env bash
set -euo pipefail

node=${1:?node is required}
root=${NVIS_P2P_ROOT:-/home/hlakshminara/NVIS-P2P}
base=/opt/containers/.nvis-p2p
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)

case "$node" in
  ink50-dh02-[a-k][0-9][0-9]-*)
    letter=${node#ink50-dh02-}; letter=${letter:0:1}
    printf -v ordinal '%d' "'$letter"
    su=$((ordinal - 96))
    ;;
  ink50-dh01-[a-c][0-9][0-9]-*)
    letter=${node#ink50-dh01-}; letter=${letter:0:1}
    printf -v ordinal '%d' "'$letter"
    su=$((ordinal - 96 + 11))
    ;;
  *) echo "UNCApped_REJECTED node=$node reason=outside-SU1-SU14" >&2; exit 64 ;;
esac

ip=$(ssh "${ssh_opts[@]}" "$node" "hostname -I | awk '{print \$1}'")
rack=${node%-*}
temporary=$(mktemp -d)
trap 'rm -rf -- "$temporary"' EXIT
sed -e "s/__NODE_IP__/$ip/g" \
    -e "s/__HOSTNAME__/$node/g" \
    -e "s/__RACK__/$rack/g" \
    -e "s/__SU__/$su/g" \
    "$root/configs/peer.template.yaml" > "$temporary/peer.yaml"

scp -q "${ssh_opts[@]}" "$temporary/peer.yaml" "$node:$base/peer.yaml.uncapped"
scp -q "${ssh_opts[@]}" "$root/scripts/dfstore-sync-catalog.sh" "$node:$base/bin/dfstore-sync-catalog.sh.uncapped"
scp -q "${ssh_opts[@]}" "$root/deploy/systemd/nvis-p2p-peer.service" "$node:$base/nvis-p2p-peer.service.uncapped"
scp -q "${ssh_opts[@]}" "$root/deploy/systemd/nvis-p2p-catalog-sync.service" "$node:$base/nvis-p2p-catalog-sync.service.uncapped"

ssh "${ssh_opts[@]}" "$node" bash -s -- "$base" <<'REMOTE'
set -euo pipefail
base=$1
sudo -n systemctl stop nvis-p2p-catalog-sync.service || true
sudo -n systemctl stop nvis-p2p-peer.service
mv "$base/peer.yaml.uncapped" "$base/peer.yaml"
mv "$base/bin/dfstore-sync-catalog.sh.uncapped" "$base/bin/dfstore-sync-catalog.sh"
chmod 0755 "$base/bin/dfstore-sync-catalog.sh"
sudo -n install -m 0644 "$base/nvis-p2p-peer.service.uncapped" /etc/systemd/system/nvis-p2p-peer.service
sudo -n install -m 0644 "$base/nvis-p2p-catalog-sync.service.uncapped" /etc/systemd/system/nvis-p2p-catalog-sync.service
sudo -n systemctl daemon-reload
# Neutralize runtime limits previously created by systemctl set-property. They
# survive unit replacement under /run/systemd/system.control otherwise.
sudo -n systemctl set-property --runtime nvis-p2p-peer.service MemoryMax=infinity
sudo -n systemctl set-property --runtime nvis-p2p-catalog-sync.service MemoryMax=infinity
sudo -n systemctl reset-failed nvis-p2p-peer.service nvis-p2p-catalog-sync.service || true
sudo -n systemctl start nvis-p2p-peer.service
for _ in $(seq 1 60); do
  [[ -S $base/run/dfdaemon.sock ]] && break
  sleep 1
done
[[ -S $base/run/dfdaemon.sock ]]
sudo -n systemctl start --no-block nvis-p2p-catalog-sync.service
REMOTE

echo "UNCAPPED_READY node=$node su=$su ip=$ip"
