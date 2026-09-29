#!/usr/bin/env bash
set -u

node=${1:?node is required}
ssh_opts=(-o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=4 -o ServerAliveCountMax=1)

if ! output=$(ssh "${ssh_opts[@]}" "$node" 'bash -s' <<'REMOTE'
set -u
ip=$(ip route get 10.41.112.3 2>/dev/null | sed -n 's/.* src \([^ ]*\).*/\1/p' | head -n1)
page=$(getconf PAGESIZE 2>/dev/null || echo unknown)
arch=$(uname -m)
free=$(df -B1 --output=avail /opt/containers 2>/dev/null | tail -n1 | tr -d ' ')
uid=$(id -u)
gid=$(id -g)
if sudo -n true >/dev/null 2>&1; then sudo_state=yes; else sudo_state=no; fi
if test -w /opt/containers; then writable=yes; else writable=no; fi
if test -S /opt/containers/.nvis-p2p/run/dfdaemon.sock; then peer=socket; else peer=absent; fi
daemon_sha=$(sha256sum /opt/containers/.nvis-p2p/bin/dfdaemon 2>/dev/null | awk '{print $1}')
active=$(pgrep -fc '/opt/containers/.nvis-p2p/bin/dfget' 2>/dev/null || true)
missing=0
ready=0
check_image() {
  file=$1 expected=$2
  if test -f "/opt/containers/$file" && test "$(stat -c %s "/opt/containers/$file")" = "$expected"; then
    ready=$((ready + 1))
  else
    missing=$((missing + expected))
  fi
}
check_image nvbandwidth-v0.9-arm.sqsh 853815296
check_image nvidia+hpc-benchmarks+26.02-arm.sqsh 9818501120
check_image mnubergemm-26.03-v1.17-arm.sqsh 13949554688
check_image nvidia+nemo+26.04.01.sqsh 42245001216
check_image nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh 42477232128
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
  "$ip" "$page" "$arch" "$free" "$uid" "$gid" "$sudo_state" "$writable" "$peer" "${daemon_sha:--}" "$active:$ready:$missing"
REMOTE
); then
  printf '%s\tunreachable\t-\t-\t-\t-\t-\t-\t-\t-\t-\t-\n' "$node"
  exit 0
fi

printf '%s\treachable\t%s\n' "$node" "$output"
