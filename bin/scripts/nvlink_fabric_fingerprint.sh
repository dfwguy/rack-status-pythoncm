#!/bin/bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# nvlink_fabric_fingerprint.sh — capture the full NVLink / MNNVL fabric state of a
# single GB200/GB300 compute node, so a known-good node can be diffed against a
# node where NCCL hangs on NVLS/MNNVL.
#
# Designed to run ON the compute node (e.g. `ssh <node> bash -s < this_script`, or
# staged on the shared FS and invoked over ssh/pdsh). Read-only except the optional
# NVLS probe (NVLS_PROBE=1), which runs a short all_reduce to prove the collective
# either completes or hangs. All output is section-delimited (=== TAG ===) so two
# captures diff cleanly.
#
# Usage:
#   bash nvlink_fabric_fingerprint.sh                 # static fabric fingerprint only
#   NVLS_PROBE=1 bash nvlink_fabric_fingerprint.sh    # + short NVLS all_reduce probe
#
# Env:
#   NVLS_PROBE=0|1     Run the NVLS collective probe (default 0)
#   NVLS_VENV          venv with a Blackwell-capable torch (default below)
#   NVLS_TEST          python all_reduce probe (default below)
#   NVLS_TIMEOUT       seconds before the probe is declared a HANG (default 120)
#
# Exit code is always 0 — this is a capture tool, not a gate (see nvlink_preflight.sh).

set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}

NVLS_PROBE="${NVLS_PROBE:-0}"
NVLS_VENV="${NVLS_VENV:-/cm/shared-ubuntu2404-aarch64/nvtest/nv-real13}"
NVLS_TEST="${NVLS_TEST:-/cm/shared-ubuntu2404-aarch64/nvtest/nccl_venv_test.py}"
NVLS_TIMEOUT="${NVLS_TIMEOUT:-120}"

sec() { printf '\n=== %s ===\n' "$1"; }

echo "########## NVLINK FABRIC FINGERPRINT ##########"
echo "host=$(hostname) date=$(date -u +%Y-%m-%dT%H:%M:%SZ) arch=$(uname -m) kernel=$(uname -r)"

sec "GPU FABRIC REGISTRATION (nvidia-smi -q -d FABRIC)"
# The decisive block: per-GPU fabric State / Status / CliqueId / ClusterUUID / Health.
# Healthy GB300: State=Completed, Status=Success, Health Mask present (0x0 on healthy fleet).
if ! nvidia-smi -q -d FABRIC 2>/dev/null | grep -iE 'Fabric|State|Status|Clique|Cluster|Health|Bandwidth|Route' ; then
  echo "(-d FABRIC unsupported; falling back to full -q grep)"
  nvidia-smi -q 2>/dev/null | grep -iE 'Fabric|State|Status|Clique|Cluster|Health' | head -40
fi

sec "GPU / DRIVER / VBIOS"
nvidia-smi --query-gpu=index,name,driver_version,vbios_version,gpu_uuid --format=csv 2>&1 | head -8

sec "NVLINK LINK STATE (GPU0 status + capabilities)"
nvidia-smi nvlink --status -i 0 2>&1 | head -25
echo "----- capabilities (GPU0) -----"
nvidia-smi nvlink -c -i 0 2>&1 | head -20

sec "NVLINK ERROR COUNTERS (all GPUs)"
nvidia-smi nvlink -e 2>&1 | grep -iE 'GPU|Replay|Recovery|CRC|error' | head -40

sec "GPU TOPOLOGY MATRIX"
nvidia-smi topo -m 2>&1 | head -14

sec "NVSWITCH DEVICES VISIBLE ON NODE"
# On NVL72 the NVSwitches live in the rack, not the node — but the driver still
# exposes nvswitch handles. "0 switches" / missing handles here is a fabric-down signal.
if [ -d /proc/driver/nvidia-nvswitch/devices ]; then
  n=$(ls -1 /proc/driver/nvidia-nvswitch/devices 2>/dev/null | wc -l)
  echo "nvswitch_proc_devices=${n}"
  ls -1 /proc/driver/nvidia-nvswitch/devices 2>/dev/null | head
else
  echo "nvswitch_proc_devices=0 (/proc/driver/nvidia-nvswitch/devices absent)"
fi
echo "nvidia-smi nvswitch query:"
nvidia-smi -q -i 0 2>/dev/null | grep -iE 'NvSwitch|Switch' | head || true

sec "IMEX SERVICE"
echo "is-active=$(systemctl is-active nvidia-imex 2>&1)"
echo "channels=$(ls -1 /dev/nvidia-caps-imex-channels/ 2>/dev/null | wc -l) ($(ls -1 /dev/nvidia-caps-imex-channels/ 2>/dev/null | paste -sd, -))"
echo "----- config.cfg keys -----"
grep -E '^(SERVER_PORT|IMEX_CMD_PORT|IMEX_CMD_ENABLED|IMEX_CMD_BIND_INTERFACE_IP)=' /etc/nvidia-imex/config.cfg 2>/dev/null | paste -sd' ' -
echo "----- nodes_config.cfg -----"
echo "md5=$(md5sum /etc/nvidia-imex/nodes_config.cfg 2>/dev/null | awk '{print $1}') lines=$(wc -l < /etc/nvidia-imex/nodes_config.cfg 2>/dev/null)"

sec "IMEX READINESS (nvidia-imex-ctl -q)"
timeout 20 nvidia-imex-ctl -q 2>&1 | head -20 || echo "(nvidia-imex-ctl -q failed/timed out)"

sec "IMEX DOMAIN / NODES (nvidia-imex-ctl -N)"
timeout 20 nvidia-imex-ctl -N 2>&1 | awk '/Nodes:/,/Domain State:/{print}' | sed -e 's/\*/ /g' | tr -s ' ' | head -40 || echo "(nvidia-imex-ctl -N failed/timed out)"

sec "KERNEL ERRORS (dmesg: NVLink / NVSwitch / Xid / NV_ERR)"
dmesg 2>/dev/null | grep -iE 'nvlink|nvidia-nvswitch|Xid|NV_ERR|fabric|GPU lost|fallen off' | tail -25 || echo "(no matching dmesg lines / dmesg unreadable)"

if [ "${NVLS_PROBE}" = "1" ]; then
  sec "NVLS COLLECTIVE PROBE (4 local GPUs, timeout ${NVLS_TIMEOUT}s)"
  if [ ! -x "${NVLS_VENV}/bin/python" ] || [ ! -f "${NVLS_TEST}" ]; then
    echo "PROBE_SKIPPED: venv (${NVLS_VENV}) or test (${NVLS_TEST}) missing"
  else
    export NCCL_NVLS_ENABLE=1 NCCL_MNNVL_ENABLE=1 NCCL_P2P_LEVEL=NVL NCCL_IB_DISABLE=1
    export NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=INIT,NVLS,GRAPH
    export NCCL_TEST_SIZES_GIB="${NCCL_TEST_SIZES_GIB:-4}" NCCL_TEST_ITERS="${NCCL_TEST_ITERS:-5}"
    probe_log="$(mktemp /tmp/nvls_probe.XXXXXX.log)"
    timeout "${NVLS_TIMEOUT}" "${NVLS_VENV}/bin/python" -m torch.distributed.run \
      --standalone --nproc_per_node=4 "${NVLS_TEST}" >"${probe_log}" 2>&1
    rc=$?
    echo "probe_rc=${rc} ($( [ "$rc" = 124 ] && echo HANG/TIMEOUT || ([ "$rc" = 0 ] && echo OK || echo ERROR) ))"
    echo "----- MNNVL fabric line(s) -----"
    grep -E 'MNNVL .*state [0-9]+ healthMask' "${probe_log}" | sed 's/.*NCCL INFO //' | sort -u | head
    echo "----- progress markers -----"
    grep -E 'NCCL version|Connected all|NVLS|Symmetric|multicast|busbw|warmup all_reduce OK|DONE' "${probe_log}" | tail -20
    echo "----- last 8 lines (where it stopped) -----"
    tail -8 "${probe_log}"
    rm -f "${probe_log}"
  fi
fi

sec "END"
echo "fingerprint_done host=$(hostname)"
exit 0
