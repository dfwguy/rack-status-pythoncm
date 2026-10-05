#!/usr/bin/env bash
set -u

node=${1:?node is required}
root=${NVIS_P2P_ROOT:-/home/hlakshminara/NVIS-P2P}
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=4 -o ServerAliveCountMax=1)

if ! result=$(ssh "${ssh_opts[@]}" "$node" 'bash -s' <<'REMOTE'
set -u
base=/opt/containers/.nvis-p2p
catalog=$base/catalog/images.tsv
ready=0
unverified=0
missing=0
bad_size=0
image_states=()
while IFS=$'\t' read -r order display_name filename expected_bytes expected_sha; do
  [[ $order == order ]] && continue
  target=/opt/containers/$filename
  receipt=$base/receipts/$filename.sha256
  if [[ ! -e $target ]]; then
    missing=$((missing + 1))
    image_states+=("$filename:missing")
  elif [[ $(stat -c %s "$target") != "$expected_bytes" ]]; then
    bad_size=$((bad_size + 1))
    image_states+=("$filename:bad-size")
  elif [[ -s $receipt ]] && grep -Fxq "$expected_sha  $target" "$receipt"; then
    ready=$((ready + 1))
    image_states+=("$filename:verified")
  else
    unverified=$((unverified + 1))
    image_states+=("$filename:present-unverified")
  fi
done < "$catalog"
active=$(pgrep -fc '/opt/containers/.nvis-p2p/bin/(dfget|dfstore)' 2>/dev/null || true)
states=$(IFS=,; printf '%s' "${image_states[*]}")
printf 'ready=%s\tunverified=%s\tmissing=%s\tbad_size=%s\tactive=%s\timages=%s' \
  "$ready" "$unverified" "$missing" "$bad_size" "$active" "$states"
REMOTE
); then
  printf '%s\tunreachable\n' "$node"
  exit 0
fi

printf '%s\t%s\n' "$node" "$result"
