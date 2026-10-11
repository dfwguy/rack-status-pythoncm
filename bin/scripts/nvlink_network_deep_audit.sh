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
# GB300 per-node NVLink/IB/network deep audit with an actual GPU data-path test.
set -uo pipefail

expected_gpus="${RACK_AUDIT_EXPECTED_GPUS:-4}"
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ibroot="${NVIS_FABRIC_SYSFS:-/sys/class/infiniband}"
codes=()
reasons=()
warnings=()
# A code is reported once however many rails trip it, and each reason is clipped: the whole
# record is one stdout line the fan-out must carry intact, and a line cut mid-JSON reads as
# "structured audit result missing" -- a failed node reported as inconclusive.
fail() {
  local c
  for c in "${codes[@]:-}"; do
    [[ "$c" == "$1" ]] && { reasons+=("${2:0:160}"); return; }
  done
  codes+=("$1"); reasons+=("${2:0:160}")
}
warn() { warnings+=("$1: ${2:0:160}"); }

# Which devices are the compute rails, and what protocol they speak, comes from the shared
# classifier -- the cluster's fabric network (NVIS_FABRIC_NETWORK: IB|RoCE|auto) and what this
# node actually exposes -- never a device-name or link-rate literal here. Hardcoding "every
# non-bond device must be 800 Gb/s" was right for IB / RoCE-IPv4 rails and wrong for GB300
# Spectrum-X RoCE-IPv6, where the rails are 200G VFs (roce_vf_r*) beside 400G management NICs
# and DOWN OVS uplink PFs (roce_r*).
fab_link=""; fab_pin=""; fab_rails=""
if [[ -r "$script_dir/lib/fabric.inc.sh" ]]; then
  # shellcheck source=lib/fabric.inc.sh
  source "$script_dir/lib/fabric.inc.sh"
  _fab_scan=$(nvis_fabric_scan 2>/dev/null || true)
  if [[ "$_fab_scan" == *NVIS_FAB_IB_HCA=* ]]; then
    eval "$_fab_scan"
    fab_link="${NVIS_FAB_LINK:-}"; fab_pin="${NVIS_FAB_HCA_PIN:-}"; fab_rails="${NVIS_FAB_IB_HCA:-}"
  fi
fi

# Rails to check: what the scan selected, plus any device the cluster's compute-plane name
# covers but that is not ACTIVE (a rail that went down must be reported, not silently dropped
# from the scan). Without the classifier, fall back to every non-bond device.
rails=()
if [[ -n "$fab_link" ]]; then
  [[ -n "$fab_rails" ]] && IFS=',' read -r -a rails <<< "$fab_rails"
  # RoCE rails are named by a shared prefix (roce_vf_r1..4); a down one is no longer selected, and
  # the classifier's pin degrades to an exact list that omits it, so add the prefix explicitly.
  # (Not for IB: mlx5_ would also sweep in the management NICs.)
  _pin_list="$fab_pin"
  if [[ "$fab_link" == roce && ${#rails[@]} -gt 1 ]]; then
    _pin_list="${_pin_list:+$_pin_list,}$(_nvis_fabric_common_prefix "${rails[@]}")"
  fi
  if [[ -n "$_pin_list" ]]; then
    IFS=',' read -r -a _pins <<< "$_pin_list"
    for dev_path in "$ibroot"/*; do
      [[ -e "$dev_path" ]] || continue
      dev=${dev_path##*/}
      [[ "$dev" == *bond* ]] && continue
      [[ " ${rails[*]:-} " == *" $dev "* ]] && continue
      for _p in "${_pins[@]}"; do
        _p=${_p#^}; _p=${_p#=}; _p=${_p%%:*}
        [[ -n "$_p" && "$dev" == "$_p"* ]] && { rails+=("$dev"); break; }
      done
    done
  fi
else
  warn fabric_scan "fabric classifier unavailable; auditing every non-bond RDMA device"
  for dev_path in "$ibroot"/*; do
    [[ -e "$dev_path" ]] || continue
    dev=${dev_path##*/}
    [[ "$dev" == *bond* ]] || rails+=("$dev")
  done
fi

# Expectations: operator override, else derived from the fabric. Rails = GPUs per node. Rate:
# IB rails are 800G NDR; RoCE rails are uniform per site (200G VFs on NC, 400G elsewhere), so a
# RoCE rail is held to the fastest rail on the node, not to a number from another site.
expected_ib_ports="${RACK_AUDIT_EXPECTED_IB_PORTS:-$expected_gpus}"
derive_rate() {
  local d r top=0
  if [[ "$fab_link" == roce ]]; then
    for d in "${rails[@]:-}"; do
      [[ -n "$d" ]] || continue
      r=$(grep -Eo '^[0-9]+' "$ibroot/$d/ports/1/rate" 2>/dev/null | head -1 || true)
      [[ "${r:-0}" -gt "$top" ]] && top=${r:-0}
    done
    printf '%s' "$top"
  else
    printf 800
  fi
}
if [[ -n "${RACK_AUDIT_EXPECTED_IB_RATE_GBPS:-}" ]]; then
  expected_ib_rate="$RACK_AUDIT_EXPECTED_IB_RATE_GBPS"
elif [[ -n "${NVIS_FABRIC_EXPECT_RATE:-}" ]]; then
  # The expectation arrives from the catalog: the cluster's own fabric.rail_rate_gbps, or its
  # platform's. A platform describes a family of sites, so its rate means nothing on a node that
  # runs the other protocol (gb300-nvl72's 800G IB against NC's 200G RoCE VFs drained healthy
  # nodes). When the stated protocol and what this node runs disagree, say so and derive instead.
  _stated=$(printf '%s' "${NVIS_FABRIC_NETWORK:-${NVIS_RDMA_LINK_TECH:-}}" | tr '[:upper:]' '[:lower:]')
  if [[ -n "$fab_link" && -n "$_stated" && "$_stated" != auto && "$_stated" != "$fab_link" ]]; then
    warn rate_expectation "expected rate ${NVIS_FABRIC_EXPECT_RATE}G ignored: configured for ${_stated}, this node runs ${fab_link}"
    expected_ib_rate=$(derive_rate)
  else
    expected_ib_rate="$NVIS_FABRIC_EXPECT_RATE"
  fi
else
  expected_ib_rate=$(derive_rate)
fi

gpu_count=$(nvidia-smi -L 2>/dev/null | grep -c '^GPU ' || true)
[[ "$gpu_count" -ne "$expected_gpus" ]] && fail gpu_count "expected ${expected_gpus} GPUs but found ${gpu_count}"

nvlink_out=$(nvidia-smi nvlink -s 2>/dev/null || true)
active_nvlinks=$(grep -Ec 'Link [0-9]+:.*(GB/s|Active)' <<< "$nvlink_out" || true)

query=$(nvidia-smi -q 2>/dev/null || true)
fabric_completed=$(grep -Eic 'State[[:space:]]*:[[:space:]]*Completed[[:space:]]*$' <<< "$query" || true)
fabric_success=$(grep -Eic 'Status[[:space:]]*:[[:space:]]*Success[[:space:]]*$' <<< "$query" || true)
if [[ "$gpu_count" -gt 0 && ( "$fabric_completed" -lt "$gpu_count" || "$fabric_success" -lt "$gpu_count" ) ]]; then
  fail nvlink_fabric "GPU fabric is not Completed/Success on all ${gpu_count} GPUs"
fi
if ! systemctl is-active --quiet nvidia-imex 2>/dev/null; then
  fail service_nvidia_imex "nvidia-imex is not active"
fi

ib_total=0
ib_active=0
ib_bad_counters=0
ib_bad_rate=0
declare -A ib_counter_before=()
for dev in "${rails[@]:-}"; do
  [[ -n "$dev" ]] || continue
  dev_path="$ibroot/$dev"
  # Logical aggregate devices (for example mlx5_bond_0) report the bond's effective rate, not
  # a rail rate; the bond's carrier is validated separately below.
  [[ "$dev" == *bond* ]] && continue
  [[ -r "$dev_path/ports/1/state" ]] || continue
  ib_total=$((ib_total + 1))
  state=$(cat "$dev_path/ports/1/state" 2>/dev/null || true)
  physical=$(cat "$dev_path/ports/1/phys_state" 2>/dev/null || true)
  if grep -q 'ACTIVE' <<< "$state" && grep -Eq 'LINK_UP|LinkUp' <<< "$physical"; then
    ib_active=$((ib_active + 1))
  else
    fail ib_port_state "${dev} is not ACTIVE/LinkUp (${state}; ${physical})"
  fi
  rate=$(cat "$dev_path/ports/1/rate" 2>/dev/null || true)
  rate_num=$(grep -Eo '^[0-9]+' <<< "$rate" || true)
  if [[ -n "$rate_num" && "$expected_ib_rate" -gt 0 && "$rate_num" -lt "$expected_ib_rate" ]]; then
    ib_bad_rate=$((ib_bad_rate + 1))
    fail ib_link_rate "${dev} rate ${rate_num}Gb/s is below expected ${expected_ib_rate}Gb/s"
  fi
  counters="$dev_path/ports/1/counters"
  for counter in link_downed symbol_error port_rcv_errors port_xmit_discards; do
    value=$(cat "$counters/$counter" 2>/dev/null || printf 0)
    [[ "$value" =~ ^[0-9]+$ ]] || value=0
    ib_counter_before["${dev}:${counter}"]="$value"
  done
done
if [[ "$ib_total" -lt "$expected_ib_ports" ]]; then
  fail ib_port_count "expected ${expected_ib_ports} ${fab_link:-IB} compute rails but found ${ib_total}"
fi

if [[ -e /proc/net/bonding/mlx5_bond_0 ]]; then
  if grep -Eq 'MII Status:[[:space:]]*down' /proc/net/bonding/mlx5_bond_0; then
    fail bond_state "mlx5_bond_0 has a down member or bond state"
  fi
elif ip link show mlx5_bond_0 >/dev/null 2>&1; then
  if ! ip link show mlx5_bond_0 | grep -q 'state UP'; then
    fail bond_state "mlx5_bond_0 is not UP"
  fi
elif [[ -r /sys/class/infiniband/mlx5_bond_0/ports/1/state ]]; then
  # GB300 exposes the aggregate as an RDMA device even when there is no Linux
  # netdev/bonding entry.  Its 400 Gb/s rate is expected; require carrier only.
  bond_state=$(cat /sys/class/infiniband/mlx5_bond_0/ports/1/state 2>/dev/null || true)
  bond_physical=$(cat /sys/class/infiniband/mlx5_bond_0/ports/1/phys_state 2>/dev/null || true)
  if ! grep -q 'ACTIVE' <<< "$bond_state" || ! grep -Eq 'LINK_UP|LinkUp' <<< "$bond_physical"; then
    fail bond_state "mlx5_bond_0 RDMA aggregate is not ACTIVE/LinkUp (${bond_state}; ${bond_physical})"
  fi
elif [[ "$fab_link" != roce ]]; then
  # A RoCE site's aggregate (if any) is not part of the compute plane; only IB nodes are expected to have it.
  warn bond_state "mlx5_bond_0 is absent"
fi

# Reuse the established full forced-NVLink/RoCE-control gate.  Prefer the actual
# Nscale shared-tree location when it exists; the legacy /cm/shared path remains
# the preflight's portable default.
data_path=unavailable
data_path_rc=0
if [[ -x /cm/shared-ubuntu2404-aarch64/nvtest/nv-real13/bin/python ]]; then
  export NVLS_VENV="${NVLS_VENV:-/cm/shared-ubuntu2404-aarch64/nvtest/nv-real13}"
  export NVLS_TEST="${NVLS_TEST:-/cm/shared-ubuntu2404-aarch64/nvtest/nccl_venv_test.py}"
fi
if [[ -x "$script_dir/nvlink_preflight.sh" ]]; then
  data_path=forced-nvlink-preflight
  preflight_log=$(mktemp /tmp/nvis-nvlink-preflight.XXXXXX.log)
  timeout "${RACK_AUDIT_NVLINK_TIMEOUT:-360}" bash "$script_dir/nvlink_preflight.sh" --full >"$preflight_log" 2>&1 || data_path_rc=$?
  if grep -Eq 'nvlink_datapath[[:space:]]+SKIPPED' "$preflight_log"; then
    data_path=skipped
  elif [[ "$data_path_rc" -ne 0 ]]; then
    detail=$(grep -E '^\[FAIL\]' "$preflight_log" | paste -sd';' - | head -c 800)
    fail nvlink_data_path "full forced-NVLink preflight exited ${data_path_rc}: ${detail:-no failure detail}"
  elif ! grep -Eq '^\[PASS\][[:space:]]+nvlink_datapath.*forced-NVLink rc=0' "$preflight_log"; then
    data_path=unverified
    warn nvlink_data_path "full preflight returned success without a forced-NVLink PASS marker"
  fi
  rm -f "$preflight_log"
else
  warn nvlink_data_path "nvlink_preflight.sh is not deployed beside this audit script"
fi

# SU7 does not mount the legacy torch NVLS venv used by nvlink_preflight.  When
# that probe is unavailable, use the already-local nvbandwidth image and force
# verified peer copies across every GPU pair.  On NVL72 those P2P paths traverse
# the NVSwitch fabric, making this a real data-path test rather than a link-state
# read.  The nested step stays inside this node's existing four-GPU allocation.
if [[ "$data_path" == skipped ]]; then
  nvbandwidth_image="${RACK_AUDIT_NVBANDWIDTH_IMAGE:-${NVIS_NODE_CONTAINER_DIR:-/tmp/containers}/nvbandwidth-v0.9-arm.sqsh}"
  srun_bin="${RACK_AUDIT_SRUN:-/cm/local/apps/slurm/current/bin/srun}"
  [[ -x "$srun_bin" ]] || srun_bin=$(command -v srun 2>/dev/null || true)
  if [[ -n "$srun_bin" && -f "$nvbandwidth_image" && -n "${SLURM_JOB_ID:-}" ]]; then
    nvbandwidth_log=$(mktemp /tmp/nvis-nvbandwidth.XXXXXX.log)
    srun_exec=("$srun_bin")
    if [[ ${EUID:-$(id -u)} -eq 0 && -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]]; then
      srun_exec=(sudo -n -E -u "$SUDO_USER" "$srun_bin")
    fi
    timeout "${RACK_AUDIT_NVLINK_TIMEOUT:-360}" "${srun_exec[@]}" \
      --overlap --nodes=1 --ntasks=1 --ntasks-per-node=1 --gres=gpu:4 \
      --container-image="$nvbandwidth_image" \
      /usr/local/bin/nvbandwidth -t device_to_device_memcpy_read_ce -b 64 -i 1 -j \
      >"$nvbandwidth_log" 2>&1 || data_path_rc=$?
    if [[ "$data_path_rc" -eq 0 ]] && grep -Eq '"status"[[:space:]]*:[[:space:]]*"Passed"' "$nvbandwidth_log"; then
      data_path=forced-nvlink-nvbandwidth
    else
      detail=$(grep -E 'ERROR|Error|error|Failed|failed|slurmstepd|srun:' "$nvbandwidth_log" | paste -sd';' - | head -c 800)
      fail nvlink_data_path "nvbandwidth forced peer-copy probe exited ${data_path_rc}: ${detail:-missing Passed status}"
      data_path=forced-nvlink-nvbandwidth-failed
    fi
    rm -f "$nvbandwidth_log"
  else
    warn nvlink_data_path "forced data-path probe unavailable (local nvbandwidth image, srun, or Slurm allocation missing)"
  fi
fi

# Cumulative fabric counters can retain old incidents across successful jobs and
# reboots.  Only a counter that increases during this audit is a current failure.
# Sampling after the forced data-path probe also catches a link that flaps under
# traffic rather than while idle.
for dev in "${rails[@]:-}"; do
  [[ -n "$dev" ]] || continue
  [[ "$dev" == *bond* ]] && continue
  counters="$ibroot/$dev/ports/1/counters"
  for counter in link_downed symbol_error port_rcv_errors port_xmit_discards; do
    before=${ib_counter_before["${dev}:${counter}"]:-0}
    after=$(cat "$counters/$counter" 2>/dev/null || printf 0)
    [[ "$after" =~ ^[0-9]+$ ]] || after=0
    if (( after > before )); then
      delta=$((after - before))
      ib_bad_counters=$((ib_bad_counters + 1))
      fail ib_counter_delta "${dev} ${counter} increased by ${delta} during audit (${before}->${after})"
    fi
  done
done

status=pass
[[ ${#warnings[@]} -gt 0 ]] && status=warning
[[ ${#codes[@]} -gt 0 ]] && status=fail
codes_joined=$(IFS=,; printf '%s' "${codes[*]}")
reasons_joined=$(IFS='; '; printf '%s' "${reasons[*]}")
warnings_joined=$(IFS='; '; printf '%s' "${warnings[*]}")

rails_joined=$(IFS=,; printf '%s' "${rails[*]:-}")
python3 - "$status" "$codes_joined" "$reasons_joined" "$warnings_joined" "$gpu_count" "$active_nvlinks" "$ib_total" "$ib_active" "$ib_bad_counters" "$ib_bad_rate" "$data_path" "$data_path_rc" "${fab_link:-unknown}" "$rails_joined" "$expected_ib_rate" <<'PY'
import json, socket, sys
(status, codes, reasons, warnings, gpu_count, nvlinks, ib_total, ib_active,
 bad_counters, bad_rate, data_path, data_path_rc, fabric, rails, expected_rate) = sys.argv[1:]
payload = {
    "schema": 1,
    "kind": "network",
    "node": socket.gethostname().split(".")[0],
    "status": status,
    "codes": [x for x in codes.split(",") if x],
    # Bounded so the record stays one intact stdout line however many rails fail.
    "reason": reasons[:700],
    "warnings": [x for x in warnings.split("; ") if x][:5],
    "metrics": {
        "fabric": fabric,
        "rails": [x for x in rails.split(",") if x],
        "expected_rail_rate_gbps": int(expected_rate or 0),
        "gpu_count": int(gpu_count or 0),
        "active_nvlinks": int(nvlinks or 0),
        "ib_ports": int(ib_total or 0),
        "ib_active_ports": int(ib_active or 0),
        "ib_bad_counters": int(bad_counters or 0),
        "ib_bad_rate_ports": int(bad_rate or 0),
        "data_path_test": data_path,
        "data_path_returncode": int(data_path_rc or 0),
    },
}
print("NVIS_DEEP_AUDIT_JSON=" + json.dumps(payload, separators=(",", ":")))
PY

[[ "$status" == fail ]] && exit 2
exit 0
