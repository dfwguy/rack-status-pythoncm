#!/usr/bin/env bash
set -euo pipefail

node=${1:?node is required}
root=${NVIS_P2P_ROOT:-/home/hlakshminara/NVIS-P2P}
base=/opt/containers/.nvis-p2p
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)
expected_daemon=90780f4a9c602707e5db5696d78a8c431cf99fa0e71915fd5158d18e61d1d531
expected_dfget=8ac10afc58dc40e5f435df978a3e75694c81319656eedbbc3bd3c88ca9050457
expected_dfstore=beeba50931647fcef20840281a8c0162a908b511a910d27bc680b0a91d3d78e9
readonly_env=$root/deploy/object-store/secrets/dfstore-readonly.env

[[ -r $readonly_env ]] || {
  echo "DEPLOY_REJECTED node=$node reason=missing-dfstore-readonly-environment" >&2
  exit 67
}
[[ $(stat -c %a "$readonly_env") == 600 ]] || {
  echo "DEPLOY_REJECTED node=$node reason=unsafe-dfstore-readonly-environment-mode" >&2
  exit 67
}

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
  *) echo "DEPLOY_REJECTED node=$node reason=outside-SU1-SU14" >&2; exit 64 ;;
esac

preflight=$("$root/scripts/preflight-catalog-node.sh" "$node")
IFS=$'\t' read -r _ ssh_state ip page arch free uid gid sudo_state writable peer daemon_sha activity <<< "$preflight"
[[ $ssh_state == reachable && $page == 65536 && $arch == aarch64 && $uid == 1024 && $gid == 1003 && $sudo_state == yes && $writable == yes ]] || {
  echo "DEPLOY_REJECTED node=$node su=$su preflight=$preflight" >&2
  exit 65
}
IFS=: read -r active_dfget ready_images missing_bytes <<< "$activity"
required_free=$((missing_bytes + 150000000000))
(( free >= required_free )) || {
  echo "DEPLOY_REJECTED node=$node su=$su reason=insufficient-space free=$free required=$required_free" >&2
  exit 66
}

rack=${node%-*}
temporary=$(mktemp -d)
trap 'rm -rf -- "$temporary"' EXIT
sed -e "s/__NODE_IP__/$ip/g" \
    -e "s/__HOSTNAME__/$node/g" \
    -e "s/__RACK__/$rack/g" \
    -e "s/__SU__/$su/g" \
    "$root/configs/peer.template.yaml" > "$temporary/peer.yaml"

if (( active_dfget > 0 )); then
  echo "DEPLOY_DEFERRED node=$node su=$su reason=active-dfget count=$active_dfget"
  exit 75
fi

ssh "${ssh_opts[@]}" "$node" "mkdir -p '$base'/{bin,cache,catalog,logs,plugins,quarantine,receipts,run,secrets,staging,storage} && chmod 0700 '$base/secrets'"
scp -q "${ssh_opts[@]}" \
  "$root/dist/client-arm64-64k-v1.4.9/dfdaemon" "$node:$base/bin/dfdaemon.next"
scp -q "${ssh_opts[@]}" \
  "$root/dist/client-arm64-64k-v1.4.9/dfget" "$node:$base/bin/dfget.next"
scp -q "${ssh_opts[@]}" \
  "$root/dist/client-arm64-64k-v1.4.9/dfstore" "$node:$base/bin/dfstore.next"
scp -q "${ssh_opts[@]}" \
  "$root/scripts/download-catalog-image.sh" "$node:$base/bin/download-catalog-image.sh.next"
scp -q "${ssh_opts[@]}" \
  "$root/scripts/dfstore-sync-catalog.sh" "$node:$base/bin/dfstore-sync-catalog.sh.next"
scp -q "${ssh_opts[@]}" \
  "$root/catalog/images.tsv" "$node:$base/catalog/images.tsv.next"
scp -q "${ssh_opts[@]}" \
  "$readonly_env" "$node:$base/secrets/dfstore-readonly.env.next"
scp -q "${ssh_opts[@]}" \
  "$temporary/peer.yaml" "$node:$base/peer.yaml.next"
scp -q "${ssh_opts[@]}" \
  "$root/deploy/systemd/nvis-p2p-peer.service" "$node:$base/nvis-p2p-peer.service.next"
scp -q "${ssh_opts[@]}" \
  "$root/deploy/systemd/nvis-p2p-catalog-sync.service" "$node:$base/nvis-p2p-catalog-sync.service.next"

ssh "${ssh_opts[@]}" "$node" bash -s -- "$base" "$expected_daemon" "$expected_dfget" "$expected_dfstore" <<'REMOTE'
set -euo pipefail
base=$1 expected_daemon=$2 expected_dfget=$3 expected_dfstore=$4

[[ $(sha256sum "$base/bin/dfdaemon.next" | awk '{print $1}') == "$expected_daemon" ]]
[[ $(sha256sum "$base/bin/dfget.next" | awk '{print $1}') == "$expected_dfget" ]]
[[ $(sha256sum "$base/bin/dfstore.next" | awk '{print $1}') == "$expected_dfstore" ]]

sudo -n systemctl stop nvis-p2p-peer.service 2>/dev/null || true
if [[ -s $base/run/dfdaemon.pid ]]; then
  old_pid=$(<"$base/run/dfdaemon.pid")
  kill -TERM "$old_pid" 2>/dev/null || true
  for _ in {1..20}; do
    kill -0 "$old_pid" 2>/dev/null || break
    sleep 0.25
  done
  kill -KILL "$old_pid" 2>/dev/null || true
fi

mv "$base/bin/dfdaemon.next" "$base/bin/dfdaemon"
mv "$base/bin/dfget.next" "$base/bin/dfget"
mv "$base/bin/dfstore.next" "$base/bin/dfstore"
mv "$base/bin/download-catalog-image.sh.next" "$base/bin/download-catalog-image.sh"
mv "$base/bin/dfstore-sync-catalog.sh.next" "$base/bin/dfstore-sync-catalog.sh"
mv "$base/catalog/images.tsv.next" "$base/catalog/images.tsv"
mv "$base/secrets/dfstore-readonly.env.next" "$base/secrets/dfstore-readonly.env"
mv "$base/peer.yaml.next" "$base/peer.yaml"
chmod 0755 "$base/bin/dfdaemon" "$base/bin/dfget" "$base/bin/dfstore" \
  "$base/bin/download-catalog-image.sh" "$base/bin/dfstore-sync-catalog.sh"
chmod 0644 "$base/catalog/images.tsv" "$base/peer.yaml"
chmod 0700 "$base/secrets"
chmod 0600 "$base/secrets/dfstore-readonly.env"
sudo -n install -m 0644 "$base/nvis-p2p-peer.service.next" /etc/systemd/system/nvis-p2p-peer.service
sudo -n install -m 0644 "$base/nvis-p2p-catalog-sync.service.next" /etc/systemd/system/nvis-p2p-catalog-sync.service
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now nvis-p2p-peer.service
# Enabling makes native dfstore reconciliation automatic after later reboots.
# Do not start it during deployment: the durable catalog imports may not yet exist.
sudo -n systemctl enable nvis-p2p-catalog-sync.service

for _ in {1..30}; do
  [[ -S $base/run/dfdaemon.sock ]] && break
  sleep 1
done
[[ -S $base/run/dfdaemon.sock ]]
sudo -n systemctl is-active --quiet nvis-p2p-peer.service
REMOTE

echo "DEPLOY_READY node=$node su=$su ip=$ip ready_images=$ready_images missing_bytes=$missing_bytes service=systemd dfstore_sync=enabled-not-started"
