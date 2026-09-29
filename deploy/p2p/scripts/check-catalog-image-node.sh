#!/usr/bin/env bash
set -u

node=${1:?node is required}
filename=${2:?canonical image filename is required}
expected_bytes=${3:?expected byte count is required}
expected_sha=${4:?expected SHA-256 is required}
base=/opt/containers/.nvis-p2p
ssh_opts=(-o BatchMode=yes -o ConnectTimeout="${NVIS_P2P_SSH_TIMEOUT:-8}" \
  -o ServerAliveInterval=4 -o ServerAliveCountMax=1)

if ! result=$(ssh "${ssh_opts[@]}" "$node" bash -s -- \
  "$base" "$filename" "$expected_bytes" "$expected_sha" <<'REMOTE'
set -u
base=$1 filename=$2 expected_bytes=$3 expected_sha=$4
target=/opt/containers/$filename
receipt=$base/receipts/$filename.sha256
pid_file=$base/run/catalog-$filename.pid
log=$base/logs/catalog-$filename.log

state=PENDING
detail=not_started
if [[ -e $target ]]; then
  actual_bytes=$(stat -c %s "$target" 2>/dev/null || printf 0)
  if [[ $actual_bytes != "$expected_bytes" ]]; then
    state=BAD_SIZE
    detail="bytes=$actual_bytes"
  elif [[ -s $receipt ]] && grep -Fxq "$expected_sha  $target" "$receipt"; then
    state=READY
    detail=receipt_valid
  else
    state=UNVERIFIED
    detail=receipt_missing_or_invalid
  fi
else
  actual_bytes=0
fi

pid=0
if [[ -s $pid_file ]]; then
  pid=$(<"$pid_file")
  if [[ $pid =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    if [[ $state != READY ]]; then
      state=ACTIVE
      detail="pid=$pid"
    fi
  elif [[ $state != READY && -s $log ]]; then
    detail=$(tail -n 1 "$log" | tr '\t\r\n' '   ' | cut -c1-240)
    [[ -n $detail ]] || detail=worker_exited
    state=FAILED
  fi
fi

printf '%s\t%s' "$state" "$detail"
REMOTE
); then
  printf '%s\tUNREACHABLE\tssh_failed\n' "$node"
  exit 0
fi

printf '%s\t%s\n' "$node" "$result"
