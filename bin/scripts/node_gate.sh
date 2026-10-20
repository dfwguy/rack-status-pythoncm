#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# node_gate.sh -- one node's part of the node gate: clean up after the last job, then check the node is fit
# for the next one. Runs as one Slurm task per node inside node_gate.sbatch, which holds every target node
# EXCLUSIVELY -- that is what makes the cleanup safe: anything still on a GPU belongs to a job that has ended.
# Never run it on a node another job is using.
#
# Why (Nscale NC Exemplar run, 2026-10-20): jobs submitted back to back on the same nodes inherited what the
# last one left. Nemotron 3 ran out of memory on GPUs still holding ~270 GiB; eight jobs in a row started on a
# GPU that had just thrown an uncorrectable ECC error and died in 90 s each; and 127 of 128 nodes carried ~1 GB
# of NCCL shared-memory segments from earlier jobs.
#
#   cleanup  kill leftover GPU processes; remove leftover pyxis containers and this user's /dev/shm files;
#            restart nvidia-imex / nvidia-persistenced / nvidia-dcgm when one is not active
#   checks   nvidia-smi answers with every GPU; no GPU memory held; no uncorrected ECC; no HW slowdown or power
#            brake; NVLink fabric Completed in one clique; no NEW hardware-fault Xid since this node's last gate
#
# Prints one line, NVIS_NODE_GATE_JSON={...}: status pass|warning|fail, codes, reason, warnings, actions -- the
# same contract as the deep audit's per-node line, so the results pipeline can read either.
#
# Usage: node_gate.sh STATE_DIR      (STATE_DIR: shared, per-node Xid baselines)
# Environment: RACK_NODE_SUDO=1 (the cluster's "run node commands with sudo"), NVIS_NODE_GATE_GPUS (expected
# GPUs per node, default: what nvidia-smi listed at the last gate, else 4), NVIS_NODE_GATE_SHM_DIR (/dev/shm),
# NVIS_NODE_GATE_KILL_WAIT (seconds after killing leftovers, 15).
set -u
state=${1:?state dir}
mkdir -p "$state" 2>/dev/null
host=$(hostname -s)
sudo_cmd=()
[[ "${RACK_NODE_SUDO:-}" == 1 ]] && sudo_cmd=(sudo -n)
shm=${NVIS_NODE_GATE_SHM_DIR:-/dev/shm}
# Xids that mean the GPU or its links are at fault (ECC/DBE, uncontained, off the bus, GSP, NVLink, reset).
crit='48|63|64|74|79|92|94|95|109|119|120|121|136|140|143|144|145|146|147|148|149|150|154|155|156|172'
codes=(); reasons=(); warnings=(); actions=()
fail() { codes+=("$1"); reasons+=("$2"); }

# --- cleanup ----------------------------------------------------------------------------------------------
pids=$(timeout 60 nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | tr -d ' ' | grep -E '^[0-9]+$' | sort -u)
if [[ -n "$pids" ]]; then
  # shellcheck disable=SC2086 # one argument per pid
  "${sudo_cmd[@]}" kill -9 $pids 2>/dev/null
  sleep "${NVIS_NODE_GATE_KILL_WAIT:-15}"
  actions+=("killed $(wc -w <<< "$pids") leftover GPU process(es)")
fi
n=0
for c in $(enroot list 2>/dev/null | grep '^pyxis_'); do enroot remove -f "$c" >/dev/null 2>&1 && n=$((n + 1)); done
((n)) && actions+=("removed $n pyxis container(s)")
s=$(find "$shm" -maxdepth 1 -user "$(id -u)" -type f 2>/dev/null | wc -l)
if ((s)); then
  find "$shm" -maxdepth 1 -user "$(id -u)" -type f -delete 2>/dev/null
  actions+=("removed $s file(s) from $shm")
fi
for svc in nvidia-imex nvidia-persistenced nvidia-dcgm; do
  systemctl list-unit-files "${svc}.service" 2>/dev/null | grep -q "^${svc}.service" || continue
  systemctl is-active --quiet "$svc" && continue
  if "${sudo_cmd[@]}" systemctl restart "$svc" 2>/dev/null && sleep 5 && systemctl is-active --quiet "$svc"; then
    actions+=("restarted $svc")
  else
    fail service-down "$svc is not active after a restart"
  fi
done

# --- checks -----------------------------------------------------------------------------------------------
expect=${NVIS_NODE_GATE_GPUS:-$(cat "$state/$host.gpus" 2>/dev/null || echo 4)}
gpus=$(timeout 60 nvidia-smi -L 2>/dev/null | grep -c '^GPU ')
if (( gpus == 0 || gpus < expect )); then
  fail gpu-missing "nvidia-smi lists ${gpus}/${expect} GPUs or does not answer"
else
  echo "$gpus" > "$state/$host.gpus" 2>/dev/null
  while IFS=, read -r i mem ecc why; do
    [[ $mem =~ ^[0-9]+$ ]] && ((mem >= 1024)) && fail gpu-memory-held "GPU$i holds ${mem} MiB after cleanup"
    [[ $ecc =~ ^[0-9]+$ ]] && ((ecc > 0)) && fail ecc-uncorrected "GPU$i has $ecc uncorrected ECC error(s)"
    [[ $why =~ ^0x[0-9a-fA-F]+$ ]] && (( why & 0x88 )) && fail power-brake "GPU$i HW slowdown / power brake ($why)"
  done < <(timeout 60 nvidia-smi --query-gpu=index,memory.used,ecc.errors.uncorrected.volatile.total,clocks_event_reasons.active \
             --format=csv,noheader,nounits 2>/dev/null | tr -d ' ')
  left=$(timeout 60 nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | grep -c '[0-9]')
  ((left)) && fail gpu-process-survived "$left GPU process(es) survived the cleanup"
  q=$(timeout 60 nvidia-smi -q 2>/dev/null)
  completed=$(grep -cE '^ +State +: Completed' <<< "$q")
  cliques=$(grep -E 'CliqueId' <<< "$q" | sort -u | grep -c .)
  if grep -q 'CliqueId' <<< "$q"; then  # NVLink-domain GPUs only
    ((completed >= gpus)) || fail fabric-not-completed "NVLink fabric Completed on ${completed}/${gpus} GPUs"
    ((cliques == 1)) || fail clique-split "GPUs report ${cliques} different CliqueIds"
  fi
fi
xid=$("${sudo_cmd[@]}" dmesg 2>/dev/null | grep -cE "NVRM: Xid \(PCI:[^)]+\): (${crit}),")
if [[ -f "$state/$host.xid" ]]; then
  before=$(cat "$state/$host.xid")
  ((xid > before)) && fail new-hw-xid "$((xid - before)) new hardware-fault Xid line(s) since the last gate"
elif ((xid > 0)); then
  warnings+=("$xid hardware-fault Xid line(s) since boot (first gate on this node: no baseline yet)")
fi
echo "$xid" > "$state/$host.xid" 2>/dev/null

# --- verdict ----------------------------------------------------------------------------------------------
status=pass
((${#warnings[@]})) && status=warning
((${#codes[@]})) && status=fail
json_list() { local first=1 x; printf '['; for x in "$@"; do ((first)) || printf ','; first=0; printf '"%s"' "${x//\"/\'}"; done; printf ']'; }
reason=$(IFS='; '; echo "${reasons[*]:-}")
printf 'NVIS_NODE_GATE_JSON={"node":"%s","status":"%s","codes":%s,"reason":"%s","warnings":%s,"actions":%s}\n' \
  "$host" "$status" "$(json_list "${codes[@]}")" "${reason//\"/\'}" "$(json_list "${warnings[@]}")" "$(json_list "${actions[@]}")"
exit 0
