#!/usr/bin/env bash
set -euo pipefail

node=${1:?node is required}
filename=${2:?canonical image filename is required}
base=/opt/containers/.nvis-p2p
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)

ssh "${ssh_opts[@]}" "$node" bash -s -- "$base" "$filename" <<'REMOTE'
set -euo pipefail
base=$1 filename=$2
[[ $filename =~ ^[A-Za-z0-9+._-]+[.]sqsh$ ]]
[[ -S $base/run/dfdaemon.sock ]]
[[ -x $base/bin/download-catalog-image.sh ]]
pid_file=$base/run/catalog-$filename.pid
if [[ -s $pid_file ]]; then
  pid=$(<"$pid_file")
  if kill -0 "$pid" 2>/dev/null; then
    echo "CATALOG_LAUNCH_ACTIVE node=$(hostname) filename=$filename pid=$pid"
    exit 0
  fi
fi
nohup "$base/bin/download-catalog-image.sh" "$filename" > "$base/logs/catalog-$filename.log" 2>&1 &
pid=$!
printf '%s\n' "$pid" > "$pid_file"
echo "CATALOG_LAUNCH_STARTED node=$(hostname) filename=$filename pid=$pid"
REMOTE
