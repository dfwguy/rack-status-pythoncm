#!/usr/bin/env bash
set -euo pipefail

base=/opt/containers/.nvis-p2p
pid_file="$base/run/dfdaemon.pid"

mkdir -p "$base"/{bin,cache,logs,plugins,run,storage}

if [[ -s "$pid_file" ]]; then
  old_pid=$(<"$pid_file")
  if [[ -r "/proc/$old_pid/cmdline" ]] && tr '\0' ' ' < "/proc/$old_pid/cmdline" | grep -Fq "$base/bin/dfdaemon"; then
    echo "NVIS-P2P peer already running with PID $old_pid"
    exit 0
  fi
fi

# A terminated daemon can leave its Unix socket behind. Remove only the stale
# socket after proving no managed daemon is alive, so readiness cannot succeed
# before the new daemon has actually bound it.
if [[ -S "$base/run/dfdaemon.sock" ]]; then
  unlink "$base/run/dfdaemon.sock"
fi

ulimit -n 65536
nohup taskset -c 0-15 ionice -c2 -n7 nice -n 10 \
  "$base/bin/dfdaemon" \
  --config "$base/peer.yaml" \
  --log-dir "$base/logs" \
  --console \
  > "$base/logs/console.log" 2>&1 &
peer_pid=$!
echo "$peer_pid" > "$pid_file"

for _ in $(seq 1 30); do
  [[ -S "$base/run/dfdaemon.sock" ]] && break
  kill -0 "$peer_pid" 2>/dev/null || {
    tail -n 80 "$base/logs/console.log" >&2
    exit 1
  }
  sleep 1
done

[[ -S "$base/run/dfdaemon.sock" ]] || {
  echo "NVIS-P2P peer socket did not become ready" >&2
  exit 1
}

echo "NVIS-P2P peer ready with PID $peer_pid"
