#!/usr/bin/env bash
# launch-catalog-image-node.sh NODE FILENAME [DOWNLOAD_TIMEOUT_SECONDS]
#
# Start a node's download of one catalog image in the background, unless one is already running.
# The download is given the limit (default NVIS_P2P_DOWNLOAD_TIMEOUT_SECONDS, else 3600) and enforces
# it itself. A "running" download older than that limit (plus NVIS_P2P_STALE_GRACE_SECONDS, default
# 60) is not running, it is hung -- on NC one sat for ten hours and every relaunch re-adopted it -- so
# it is stopped and a fresh one started in its place.
set -euo pipefail

node=${1:?node is required}
filename=${2:?canonical image filename is required}
download_timeout=${3:-${NVIS_P2P_DOWNLOAD_TIMEOUT_SECONDS:-3600}}
base=${NVIS_P2P_BASE:-/opt/containers/.nvis-p2p}
# Slack past the limit before a running download counts as stale: the download's own kill lands up to
# a minute after its limit, and that is not the moment to start a second one.
stale_grace=${NVIS_P2P_STALE_GRACE_SECONDS:-60}
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=2)
[[ $download_timeout =~ ^[1-9][0-9]*$ ]] || { echo "CATALOG_LAUNCH_BAD_TIMEOUT value=$download_timeout" >&2; exit 64; }
[[ $stale_grace =~ ^[0-9]+$ ]] || { echo "CATALOG_LAUNCH_BAD_GRACE value=$stale_grace" >&2; exit 64; }

ssh "${ssh_opts[@]}" "$node" bash -s -- "$base" "$filename" "$download_timeout" "$stale_grace" <<'REMOTE'
set -euo pipefail
base=$1 filename=$2 download_timeout=$3 stale_grace=$4
[[ $filename =~ ^[A-Za-z0-9+._-]+[.]sqsh$ ]] || { echo "CATALOG_LAUNCH_REJECTED filename=$filename" >&2; exit 64; }
[[ -S $base/run/dfdaemon.sock ]] || { echo "CATALOG_LAUNCH_REJECTED reason=no-peer-socket" >&2; exit 69; }
[[ -x $base/bin/download-catalog-image.sh ]] || { echo "CATALOG_LAUNCH_REJECTED reason=no-download-script" >&2; exit 69; }
pid_file=$base/run/catalog-$filename.pid
replaced=""
if [[ -s $pid_file ]]; then
  pid=$(<"$pid_file")
  if [[ $pid =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    age=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')
    if [[ ${age:-0} =~ ^[0-9]+$ ]] && (( age <= download_timeout + stale_grace )); then
      echo "CATALOG_LAUNCH_ACTIVE node=$(hostname) filename=$filename pid=$pid age=${age}s"
      exit 0
    fi
    # Older than any download is allowed to be: stop it (its dfget first) and start over.
    pkill -TERM -P "$pid" 2>/dev/null || true
    kill -TERM "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    pkill -KILL -P "$pid" 2>/dev/null || true
    kill -KILL "$pid" 2>/dev/null || true
    replaced=" replaced_stale_pid=$pid age=${age}s"
  fi
fi
NVIS_P2P_DOWNLOAD_TIMEOUT_SECONDS=$download_timeout NVIS_P2P_BASE=$base \
  nohup "$base/bin/download-catalog-image.sh" "$filename" > "$base/logs/catalog-$filename.log" 2>&1 &
pid=$!
printf '%s\n' "$pid" > "$pid_file"
echo "CATALOG_LAUNCH_STARTED node=$(hostname) filename=$filename pid=$pid timeout=${download_timeout}s$replaced"
REMOTE
