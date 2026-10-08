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
# nvlink_preflight.sh — per-node NVLink / NVLS readiness gate for GB200/GB300 NVL72.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Catches the node states that make NCCL NVLS/MNNVL collectives HANG, BEFORE you
# launch a job. ROOT CAUSE (MEL01, confirmed): on an NVL72 there is NO direct
# GPU<->GPU NVLink — every GPU's 18 NVLink5 ports go up into the rack's L1 NVSwitch
# trays, so even a 2-GPU same-node collective routes GPU -> NVSwitch -> GPU. When the
# NVSwitch SDN routing (programmed by NMX-C / GFM) is unprogrammed or wedged, EVERY
# NVLink path deadlocks while RoCE/IB still works (it bypasses NVLink). This is a
# *switch-side* fault and is INVISIBLE to every node-side static read:
#   - nvidia-smi Fabric "Completed/Success/Healthy"  proves registration, not a route
#   - IMEX active / Domain UP / CliqueId correct      proves control plane, not data path
#   - 18 NVLinks "up" (fingerprints match good node)   proves PHY link-up, not SDN routing
# So static fields do NOT predict the hang. The ONLY reliable detector is a functional
# data-path probe that FORCES the NVLink path under a hard timeout, with RoCE as the
# control leg (exactly the by-hand test that isolated it: 2-GPU NVLink hangs, 2-GPU
# forced onto RoCE works). See docs/NVLINK_FABRIC_DIAGNOSIS_2026-06-28.md.
#
# Checks:
#   1. node responds + all 4 GPUs enumerate          (Class B: NVLink-down nodes go "Not responding")
#   2. 18 NVLinks up + GRES links valid (not -1,0,0,0) (Class B: links not enumerated)
#   2b. P2P matrix (nvidia-smi topo -p2p n) all OK    (NS/TNS/CNS = routing/partition broken)
#   3. Fabric State=Completed / Status=Success / Health=Healthy / Route Unhealthy=False
#   4. IMEX active + READY + Domain UP + no disconnected peer (no 'D' in the matrix)
#   5a. dmesg clean of SWITCH-SIDE link faults: Xid 149 (NETIR_LINK_DOWN) / Xid 154 /
#       knvlinkSetDegradedMode / nvlink down|degraded (Class B/D -> escalate FABRIC)
#   5b. dmesg clean of NV_ERR_INVALID_STATE / NV_ERR_NOT_SUPPORTED / GPU-lost
#                                                     (Class A/C: dirty GPU/channel runtime state)
#   5c. (non-fatal) SHARP `sharp_hello` per rail HCA -> the SHARP Aggregation Manager
#       (in-network reduction / CollNet) is reachable on every rail. WARN (not FAIL) when
#       SHARP isn't provisioned on the fabric — NCCL simply falls back to NVLS_TREE — so it
#       never blocks a healthy RoCE node. Set SHARP_STRICT=1 to make it a hard gate. Skips
#       cleanly if sharp_hello is absent.
#   6. (default ON) FUNCTIONAL GATE: forced-NVLink all_reduce under timeout; on hang,
#      a forced-RoCE control leg. NVLink-hang + RoCE-pass = NVSwitch SDN routing dead
#      -> escalate to FABRIC (nvswitch_sdn_reset + nvswitches_configure_nmx_c), the node
#      itself is fine. This is the signal all the static checks miss.
# Mirrors the manual node-side checklist (nvidia-smi -q Fabric; topo -p2p n; dmesg
# NVRM/Xid/nvlink; nvidia-bug-report.sh on FAIL).
#
# Excluders: checks 1 (GPU enumerate) and 3 (fabric register) are hard prerequisites — if
# either is BAD the node is disqualified and the expensive step-6 probe is SKIPPED (fail-fast).
# Fabric register is necessary-not-sufficient (it read Completed/Success on a node that still
# hung), but a bad value is still a definitive excluder, so it earns its place in the gate.
#
# Exit 0 = PASS (node is NVLS-ready); Exit 1 = FAIL (drain/reset before using in a job).
#
# Usage (single node):
#   bash nvlink_preflight.sh                 # full gate incl. NVLS probe
#   bash nvlink_preflight.sh --quick         # skip the NVLS probe (fast fleet sweep)
#
# Usage (whole nodelist, from the head):
#   pdsh -R ssh -w '<nodes>' 'bash /cm/shared/nvtest/nvlink_preflight.sh --quick' | dshbak -c
#
# Env: NVLS_VENV, NVLS_TEST, NVLS_TIMEOUT (see nvlink_fabric_fingerprint.sh).
#      SHARP check (two-stage): Stage 1 filters candidate rails = active RDMA ports (any device
#      name, bonds excluded) on an RDMA
#      link layer (InfiniBand OR Ethernet/RoCE); Stage 2 runs sharp_hello and the passers are the
#      SHARP rails. Gate = SHARP rails >= #GPUs (one compute rail per GPU), so the Ethernet
#      BlueField-3 storage plane is simply not counted (no false WARN) and it works on GB200/
#      GB300/B200/B300 with no hard-coded mlx5 list. On Boston GB200 the SHARP rails resolve to
#      "mlx5_0 mlx5_1 mlx5_4 mlx5_5" (4 = #GPUs -> PASS). Env: SHARP_ENABLE (default 1),
#      SHARP_HELLO (path; autodetected under /opt/hpcx|/opt/mellanox), SHARP_HCAS (override the
#      candidate set), SHARP_EXPECT (override the expected rail count; default #GPUs),
#      SHARP_STRICT (default 0 -> WARN on shortfall; 1 -> FAIL), SHARP_TIMEOUT (default 15s/HCA).

set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}

QUICK=0
[ "${1:-}" = "--quick" ] && QUICK=1
[ "${1:-}" = "--full" ] && QUICK=0

NVLS_VENV="${NVLS_VENV:-/cm/shared/nvtest/nv-real13}"
NVLS_TEST="${NVLS_TEST:-/cm/shared/nvtest/nccl_venv_test.py}"
NVLS_TIMEOUT="${NVLS_TIMEOUT:-120}"
EXPECT_LINKS="${EXPECT_LINKS:-18}"
EXPECT_GPUS="${EXPECT_GPUS:-4}"

HOST=$(hostname)
fail=0
excluder_fail=0   # set by foundational checks (GPU enumerate, fabric register); short-circuits the probe
note() { printf '  %-26s %s\n' "$1" "$2"; }
pass_line() { printf '[PASS] %-22s %s\n' "$1" "$2"; }
fail_line() { printf '[FAIL] %-22s %s\n' "$1" "$2"; fail=1; }
# excluder: a hard prerequisite. If it fails the node is already disqualified, so we still
# run the cheap static checks (to show WHY) but skip the expensive functional probe.
excl_line() { printf '[FAIL] %-22s %s\n' "$1" "$2"; fail=1; excluder_fail=1; }

# imex_probe: version-tolerant IMEX domain reader. All driver-specific output parsing lives
# HERE so the gate logic (check 4) stays format-agnostic; add new formats to this one function.
# Sets globals:
#   IMEX_ACTIVE   systemd unit state (active/inactive/unknown)
#   IMEX_READY    `nvidia-imex-ctl -q` overall status, whitespace-stripped (READY/...)
#   IMEX_SELF_UP  1 if this node's domain is up, else 0. Handles both:
#                   - legacy: a literal "Domain State: UP" line
#                   - 580.x : per-node table, local node marked '*' with status READY
#   IMEX_DISC     count of 'D' (connected-then-hard-dropped) cells in the connectivity matrix
#                 = a real fault. R/I toward a powered-off peer are NOT counted here.
#   IMEX_UNAVAIL  count of peers reported UNAVAILABLE (down/unreachable) — degrades the domain
#                 but is that peer's fault, not this node's.
imex_probe() {
  local ntbl
  IMEX_ACTIVE=$(systemctl is-active nvidia-imex 2>/dev/null || echo unknown)
  IMEX_READY=$(timeout 20 nvidia-imex-ctl -q 2>/dev/null | tr -d '[:space:]')
  ntbl=$(timeout 20 nvidia-imex-ctl -N 2>/dev/null)
  if echo "$ntbl" | grep -qE 'Domain State: UP|\*.*READY'; then IMEX_SELF_UP=1; else IMEX_SELF_UP=0; fi
  IMEX_DISC=$(echo "$ntbl" | awk '/Nodes From.To/{m=1;next} m&&/^[[:space:]]*[0-9]+[[:space:]]/{for(i=2;i<=NF;i++) if($i=="D") d++} END{print d+0}')
  IMEX_UNAVAIL=$(echo "$ntbl" | grep -c 'UNAVAILABLE')
}

echo "### nvlink_preflight ${HOST} (quick=${QUICK}) ###"

# 1) GPUs enumerate  [EXCLUDER]
ngpu=$(nvidia-smi -L 2>/dev/null | grep -c '^GPU') || ngpu=0
if [ "${ngpu}" -ge "${EXPECT_GPUS}" ]; then
  pass_line "gpus_enumerate" "${ngpu}/${EXPECT_GPUS}"
else
  excl_line "gpus_enumerate" "${ngpu}/${EXPECT_GPUS} (node not responding / driver fault)"
fi

# 2) NVLink link count + GRES links valid
links=$(nvidia-smi nvlink --status -i 0 2>/dev/null | grep -c 'GB/s') || links=0
if [ "${links}" -ge "${EXPECT_LINKS}" ]; then
  pass_line "nvlink_links" "${links}/${EXPECT_LINKS} up @ GPU0"
else
  fail_line "nvlink_links" "${links}/${EXPECT_LINKS} up @ GPU0 (NVLink down / not fully connected)"
fi
# GRES links sanity: a faulted node reports "-1,0,0,0". nvidia-smi topo NV count per GPU must be >0.
nv_peers=$(nvidia-smi topo -m 2>/dev/null | awk '/^GPU0/{for(i=2;i<=5;i++) if($i ~ /NV/) c++} END{print c+0}')
if [ "${nv_peers:-0}" -ge 3 ]; then
  pass_line "nvlink_p2p_peers" "GPU0 sees ${nv_peers} NV peers"
else
  fail_line "nvlink_p2p_peers" "GPU0 sees ${nv_peers:-0} NV peers (expect 3; GRES links invalid)"
fi
# 2b) P2P NVLink accessibility matrix: every off-diagonal cell must read OK (not NS/CNS/TNS/GNS/U).
#     A wedged routing/partition shows up as a non-OK token where a link exists. The flag is
#     `-p2p n` / `--p2pstatus n` (NOT `--p2p`); rows are space-indented " GPUx X OK OK OK".
#     If the driver lacks the option, skip (informational) rather than false-fail.
p2p=$(nvidia-smi topo -p2p n 2>&1)
if echo "$p2p" | grep -qiE 'not recognized|invalid|unknown option|^Usage'; then
  note "nvlink_p2p_matrix" "SKIPPED (nvidia-smi topo -p2p unsupported on this driver)"
else
  p2p_bad=$(echo "$p2p" | awk '/^[[:space:]]*GPU[0-9]/{for(i=2;i<=NF;i++) if($i!="X" && $i!="OK" && $i ~ /^[A-Z]/) b++} END{print b+0}')
  if echo "$p2p" | grep -qw 'OK' && [ "${p2p_bad:-0}" -eq 0 ]; then
    pass_line "nvlink_p2p_matrix" "all GPU-GPU P2P=OK (topo -p2p n)"
  else
    bad_tok=$(echo "$p2p" | awk '/^[[:space:]]*GPU[0-9]/{for(i=2;i<=NF;i++) if($i!="X" && $i!="OK" && $i ~ /^[A-Z]/) print $i}' | sort | uniq -c | tr '\n' ' ')
    fail_line "nvlink_p2p_matrix" "${p2p_bad:-?} non-OK cell(s): ${bad_tok}(P2P NVLink routing/partition broken)"
  fi
fi

# 3) GPU fabric registration (all GPUs)  [EXCLUDER]
#    Not sufficient on its own (it reported Completed/Success on a node that still hung), but a
#    BAD value here disqualifies the node outright -> short-circuit before the expensive probe.
fab=$(nvidia-smi -q 2>/dev/null | awk '/^[[:space:]]*Fabric[[:space:]]*$/{f=1;next} f&&/State/{print "S="$NF} f&&/Status/{print "ST="$NF} f&&/Summary/{print "H="$NF} f&&/Route Unhealthy/{print "R="$NF; f=0}')
n_state_ok=$(echo "$fab"  | grep -c 'S=Completed')
n_stat_ok=$(echo "$fab"   | grep -c 'ST=Success')
n_health_ok=$(echo "$fab" | grep -c 'H=Healthy')
n_route_bad=$(echo "$fab" | grep -c 'R=True')
if [ "${n_state_ok}" -ge "${EXPECT_GPUS}" ] && [ "${n_stat_ok}" -ge "${EXPECT_GPUS}" ] && [ "${n_health_ok}" -ge "${EXPECT_GPUS}" ] && [ "${n_route_bad}" -eq 0 ]; then
  pass_line "gpu_fabric_register" "State=Completed Status=Success Health=Healthy x${EXPECT_GPUS}"
else
  excl_line "gpu_fabric_register" "completed=${n_state_ok} success=${n_stat_ok} healthy=${n_health_ok} route_unhealthy=${n_route_bad} (FM control plane not established -> In Progress/Failed/Unconfigured)"
fi

# 4) IMEX active + READY + self domain UP + no hard-disconnected peer.
#    All version-specific output parsing is encapsulated in imex_probe (helper above); this
#    block only interprets the normalized IMEX_* globals, so new formats never touch the gate.
imex_probe
if [ "${IMEX_ACTIVE}" = "active" ] && [ "${IMEX_READY}" = "READY" ] && [ "${IMEX_SELF_UP}" -ge 1 ] && [ "${IMEX_DISC:-0}" -eq 0 ]; then
  if [ "${IMEX_UNAVAIL:-0}" -gt 0 ]; then
    pass_line "imex_domain" "active READY self=UP disconnected=0 (WARN: ${IMEX_UNAVAIL} peer(s) UNAVAILABLE/down -> domain degraded)"
  else
    pass_line "imex_domain" "active READY self=UP disconnected_peers=0"
  fi
else
  fail_line "imex_domain" "active=${IMEX_ACTIVE} ready=${IMEX_READY} self_up=${IMEX_SELF_UP} disconnected=${IMEX_DISC:-?} unavailable=${IMEX_UNAVAIL:-?}"
fi

# 5) dmesg — split into two distinct signatures:
#   5a) SWITCH-SIDE link faults: Xid 149 (NETIR_LINK_DOWN) / Xid 154 / knvlinkSetDegradedMode /
#       generic nvlink down|degraded -> link training negotiated a degraded state from a
#       switch-side parameter mismatch == Class B/D, escalate FABRIC (not a node reset).
dm=$(dmesg 2>/dev/null)
link_hits=$(echo "$dm" | grep -icE 'Xid[^0-9]*(149|154)|NETIR_LINK_DOWN|knvlinkSetDegradedMode|nvlink.*(down|degrad|error)')
if [ "${link_hits}" -eq 0 ]; then
  pass_line "dmesg_nvlink_link" "no Xid149/154 / degraded-link / NVLink-down"
else
  lsample=$(echo "$dm" | grep -oE 'Xid[^,]*(149|154)|NETIR_LINK_DOWN|knvlinkSetDegradedMode' | sort | uniq -c | tr '\n' ' ')
  fail_line "dmesg_nvlink_link" "${link_hits} switch-side link fault(s): ${lsample}(link down/degraded -> NVSwitch SDN/param mismatch -> escalate FABRIC)"
fi
#   5b) DIRTY GPU channel / runtime faults: NV_ERR_INVALID_STATE / NV_ERR_NOT_SUPPORTED / GPU-lost
#       / other Xid == Class A/C, clears with gpu_reset.sh / power-cycle.
rm_hits=$(echo "$dm" | grep -icE 'NV_ERR_INVALID_STATE|NV_ERR_NOT_SUPPORTED|GPU has fallen off|GPU lost')
if [ "${rm_hits}" -eq 0 ]; then
  pass_line "dmesg_rm_clean" "no NV_ERR/GPU-lost runtime faults"
else
  sample=$(echo "$dm" | grep -oE 'NV_ERR_INVALID_STATE|NV_ERR_NOT_SUPPORTED|Xid[^ ]*' | sort | uniq -c | tr '\n' ' ')
  fail_line "dmesg_rm_clean" "${rm_hits} hit(s): ${sample}(GPU/channel dirty state -> gpu_reset.sh / power-cycle)"
fi

# 5c) SHARP / in-network reduction sanity — two-stage funnel:
#       Stage 1 (filter): candidate rails = mlx5 ports that are state=ACTIVE and carry an RDMA
#                         link layer (InfiniBand OR Ethernet/RoCE). This drops down/absent ports.
#       Stage 2 (probe):  `sharp_hello -d <hca>` handshakes the SHARP Aggregation Manager on each
#                         candidate; the ports that return "Test Passed" ARE the SHARP rails.
#     A node is expected to expose ONE SHARP rail per GPU (the GPU-attached compute NICs), so we
#     gate on (SHARP rails >= #GPUs), NOT on "every HCA passes". That is what makes it platform-
#     agnostic and noise-free: the Ethernet BlueField-3 storage/converged plane (which has no
#     SHARP tree) simply isn't counted as a rail instead of tripping a false WARN.
#     NON-FATAL by default (SHARP not provisioned -> NCCL falls back to NVLS_TREE); SHARP_STRICT=1
#     promotes a shortfall to a hard FAIL. Override the candidate set with SHARP_HCAS if needed.
if [ "${SHARP_ENABLE:-1}" = "1" ]; then
  SHARP_HELLO="${SHARP_HELLO:-}"
  if [ -z "${SHARP_HELLO}" ]; then
    for _c in /opt/hpcx/sharp/bin/sharp_hello /opt/mellanox/sharp/bin/sharp_hello "$(command -v sharp_hello 2>/dev/null)"; do
      [ -n "${_c}" ] && [ -x "${_c}" ] && { SHARP_HELLO="${_c}"; break; }
    done
  fi
  if [ -z "${SHARP_HELLO}" ] || [ ! -x "${SHARP_HELLO}" ]; then
    note "sharp_hello" "SKIPPED (sharp_hello not found; set SHARP_HELLO=/opt/hpcx/sharp/bin/sharp_hello)"
  else
    # Stage 1 — filter candidate RDMA rails (IB or RoCE), skipping DOWN/absent ports.
    if [ -n "${SHARP_HCAS:-}" ]; then
      _hcas="${SHARP_HCAS}"
    else
      _hcas=""
      # Any RDMA device name, not just mlx5_*: GB300 Spectrum-X exposes its compute rails as SR-IOV
      # VFs named roce_vf_r1..4, which an ^mlx5_ filter never offered to sharp_hello. Bonds are never
      # a GPU rail. sharp_hello below is still what decides which candidates are SHARP rails.
      for _d in $(ls /sys/class/infiniband 2>/dev/null | grep -v bond | sort -V); do
        _ll=$(cat "/sys/class/infiniband/${_d}/ports/1/link_layer" 2>/dev/null)
        _st=$(cat "/sys/class/infiniband/${_d}/ports/1/state" 2>/dev/null)
        printf '%s' "${_st}" | grep -q 'ACTIVE' || continue
        case "${_ll}" in InfiniBand|Ethernet) _hcas="${_hcas}${_d} " ;; esac
      done
    fi
    if [ -z "${_hcas// /}" ]; then
      note "sharp_hello" "SKIPPED (no active IB/RoCE HCAs found)"
    else
      # Stage 2 — probe SHARP on each candidate; passers are the SHARP rails.
      s_rails=""; s_ok=0; s_none=""
      for _hca in ${_hcas}; do
        if timeout "${SHARP_TIMEOUT:-15}" "${SHARP_HELLO}" -d "${_hca}" 2>/dev/null | grep -q "Test Passed"; then
          s_ok=$((s_ok+1)); s_rails="${s_rails}${_hca} "
        else
          s_none="${s_none}${_hca} "
        fi
      done
      # Expect one SHARP rail per GPU (compute NIC per GPU). Fall back to >=1 if GPU count unknown.
      _want="${SHARP_EXPECT:-${ngpu:-0}}"; [ "${_want}" -lt 1 ] && _want=1
      if [ "${s_ok}" -ge "${_want}" ]; then
        pass_line "sharp_hello" "SHARP on ${s_ok}/${_want} GPU rail(s): ${s_rails}(non-SHARP ports: ${s_none:-none})"
      elif [ "${SHARP_STRICT:-0}" = "1" ]; then
        fail_line "sharp_hello" "only ${s_ok}/${_want} GPU rails have SHARP (rails: ${s_rails:-none}; no SHARP on: ${s_none:-none}) -> SHARP AM unreachable/not provisioned, escalate FABRIC/SHARP"
      else
        printf '[WARN] %-22s %s\n' "sharp_hello" "only ${s_ok}/${_want} GPU rails have SHARP (rails: ${s_rails:-none}; no SHARP on: ${s_none:-none}); SHARP likely not provisioned -> NCCL falls back to NVLS_TREE. SHARP_STRICT=1 to enforce"
      fi
    fi
  fi
fi

# 6) FUNCTIONAL GATE: forced-NVLink probe under timeout, RoCE control leg on hang.
#    A 2-GPU run is enough to exercise the GPU->NVSwitch->GPU path; if the switch SDN
#    routing is dead it deadlocks here in seconds rather than in your real job.
run_probe() {  # $1=label  $2..=extra NCCL env (KEY=VAL); echoes "rc<TAB>busbw"
  local label="$1"; shift
  local plog; plog="$(mktemp /tmp/probe_${label}.XXXXXX.log)"
  ( export NCCL_DEBUG=WARN \
           NCCL_TEST_SIZES_GIB="${NCCL_TEST_SIZES_GIB:-1}" NCCL_TEST_ITERS="${NCCL_TEST_ITERS:-3}"
    for kv in "$@"; do export "${kv?}"; done
    exec timeout "${NVLS_TIMEOUT}" "${NVLS_VENV}/bin/python" -m torch.distributed.run \
      --standalone --nproc_per_node="${PROBE_GPUS}" "${NVLS_TEST}" ) >"${plog}" 2>&1
  local rc=$?
  local bw; bw=$(grep -oE 'busbw=[ ]*[0-9.]+ GB/s' "${plog}" | tail -1)
  rm -f "${plog}"
  printf '%s\t%s' "${rc}" "${bw}"
}

if [ "${QUICK}" -eq 0 ] && [ "${excluder_fail}" -ne 0 ]; then
  note "nvlink_datapath" "SKIPPED (excluder failed: GPU enumerate / fabric register bad -> no point probing)"
elif [ "${QUICK}" -eq 0 ]; then
  PROBE_GPUS="${PROBE_GPUS:-2}"   # 2 GPUs is enough to hit the NVSwitch fabric path
  if [ -x "${NVLS_VENV}/bin/python" ] && [ -f "${NVLS_TEST}" ]; then
    # Leg A: FORCE NVLink/NVLS, disable every fallback so a dead switch can't be masked.
    IFS=$'\t' read -r nrc nbw < <(run_probe nvl \
      NCCL_NVLS_ENABLE=1 NCCL_MNNVL_ENABLE=1 NCCL_P2P_LEVEL=NVL \
      NCCL_P2P_DISABLE=0 NCCL_SHM_DISABLE=1 NCCL_NET_DISABLE=1 NCCL_IB_DISABLE=1)
    if [ "${nrc}" -eq 0 ]; then
      pass_line "nvlink_datapath" "forced-NVLink rc=0 ${nbw}"
    else
      # Leg B (control): FORCE RoCE/IB, NVLink off. Distinguishes switch fault from node fault.
      IFS=$'\t' read -r rrc rbw < <(run_probe roce \
        NCCL_NVLS_ENABLE=0 NCCL_MNNVL_ENABLE=0 NCCL_P2P_DISABLE=1 \
        NCCL_SHM_DISABLE=1 NCCL_NET_DISABLE=0 NCCL_IB_DISABLE=0)
      nhint=$([ "${nrc}" -eq 124 ] && echo "HANG/TIMEOUT(${NVLS_TIMEOUT}s)" || echo "rc=${nrc}")
      if [ "${rrc}" -eq 0 ]; then
        fail_line "nvlink_datapath" "forced-NVLink ${nhint} but forced-RoCE rc=0 ${rbw} => NVSwitch SDN routing DEAD (node OK) -> escalate FABRIC: nvswitch_sdn_reset + nvswitches_configure_nmx_c"
      else
        rhint=$([ "${rrc}" -eq 124 ] && echo "HANG/TIMEOUT" || echo "rc=${rrc}")
        fail_line "nvlink_datapath" "forced-NVLink ${nhint} AND forced-RoCE ${rhint} => node/driver fault (not just switch) -> gpu_reset / power-cycle"
      fi
    fi
  else
    note "nvlink_datapath" "SKIPPED (venv/test missing: ${NVLS_VENV})"
  fi
fi

if [ "${fail}" -eq 0 ]; then
  echo "RESULT ${HOST}: PASS (NVLS-ready)"
else
  echo "RESULT ${HOST}: FAIL (do not use until drained/reset)"
  echo "       for escalation, capture a full dump: sudo nvidia-bug-report.sh -o /tmp/nvbr_${HOST}"
fi
exit "${fail}"
