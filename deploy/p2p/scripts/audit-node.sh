#!/usr/bin/env bash
set -u

node=${1:?node is required}
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=5 -o ServerAliveCountMax=1)

if output=$(ssh "${ssh_opts[@]}" "$node" '
  base=/opt/containers/.nvis-p2p
  pid=$(cat "$base/run/dfdaemon.pid") || exit 11
  kill -0 "$pid" 2>/dev/null || exit 12
  daemon_sha=$(sha256sum "$base/bin/dfdaemon" | awk "{print \$1}")
  dfget_sha=$(sha256sum "$base/bin/dfget" | awk "{print \$1}")
  rss=$(awk "/VmRSS:/ {print \$2 * 1024}" "/proc/$pid/status")
  threads=$(awk "/Threads:/ {print \$2}" "/proc/$pid/status")
  target=absent
  test -e /opt/containers/nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh && target=$(stat -c %s /opt/containers/nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh)
  printf "%s\t%s\t%s\t%s\t%s\n" "$daemon_sha" "$dfget_sha" "$rss" "$threads" "$target"
' 2>/dev/null); then
  printf '%s\thealthy\t%s\n' "$node" "$output"
else
  printf '%s\tunhealthy\t-\t-\t-\t-\t-\n' "$node"
fi
