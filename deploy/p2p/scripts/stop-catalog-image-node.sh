#!/usr/bin/env bash
# stop-catalog-image-node.sh NODE FILENAME -- stop a node's download of one catalog image.
#
# The rollout calls this for a download that has stopped making progress, so that a relaunch starts
# a fresh dfget (and the scheduler picks new parents) instead of the wave waiting out its timeout on
# a transfer that will not finish. Pieces the peer already holds stay in its storage, so the
# relaunch does not start from zero; the old partial is quarantined by the next download, as for any
# other relaunch. Prints one line: CATALOG_STOPPED (it was running) or CATALOG_NOT_RUNNING.
set -euo pipefail

node=${1:?node is required}
filename=${2:?canonical image filename is required}
base=${NVIS_P2P_BASE:-/opt/containers/.nvis-p2p}
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)

ssh "${ssh_opts[@]}" "$node" bash -s -- "$base" "$filename" <<'REMOTE'
set -euo pipefail
base=$1 filename=$2
# Explicit, not left to `set -e`: a bare failing [[ ]] does not stop every bash (3.2 carries on).
[[ $filename =~ ^[A-Za-z0-9+._-]+[.]sqsh$ ]] || { echo "CATALOG_STOP_REJECTED filename=$filename" >&2; exit 64; }
pid_file=$base/run/catalog-$filename.pid
pid=$(cat "$pid_file" 2>/dev/null || true)
if [[ ! $pid =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$pid_file"
  echo "CATALOG_NOT_RUNNING node=$(hostname) filename=$filename"
  exit 0
fi
# The download script's children (dfget) first, so the script cannot go on to verify a dead transfer.
pkill -TERM -P "$pid" 2>/dev/null || true
kill -TERM "$pid" 2>/dev/null || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
  kill -0 "$pid" 2>/dev/null || break
  sleep 1
done
pkill -KILL -P "$pid" 2>/dev/null || true
kill -KILL "$pid" 2>/dev/null || true
rm -f "$pid_file"
echo "CATALOG_STOPPED node=$(hostname) filename=$filename pid=$pid"
REMOTE
