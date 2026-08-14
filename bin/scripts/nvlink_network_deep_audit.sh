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
expected_ib_ports="${RACK_AUDIT_EXPECTED_IB_PORTS:-4}"
expected_ib_rate="${RACK_AUDIT_EXPECTED_IB_RATE_GBPS:-800}"
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
codes=()
reasons=()
warnings=()
fail() { codes+=("$1"); reasons+=("$2"); }
warn() { warnings+=("$1: $2"); }

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
for dev_path in /sys/class/infiniband/*; do
  [[ -e "$dev_path" ]] || continue
  dev=${dev_path##*/}
  # Logical aggregate devices (for example mlx5_bond_0) are expected to report
  # the bond's effective rate, not the 800 Gb/s physical-rail rate.  Validate
  # their carrier separately below and keep the physical-rail gate to HCAs.
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
  fail ib_port_count "expected ${expected_ib_ports} IB ports but found ${ib_total}"
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
else
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
  nvbandwidth_image="${RACK_AUDIT_NVBANDWIDTH_IMAGE:-/tmp/containers/nvbandwidth-v0.9-arm.sqsh}"
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
for dev_path in /sys/class/infiniband/*; do
  [[ -e "$dev_path" ]] || continue
  dev=${dev_path##*/}
  [[ "$dev" == *bond* ]] && continue
  counters="$dev_path/ports/1/counters"
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

python3 - "$status" "$codes_joined" "$reasons_joined" "$warnings_joined" "$gpu_count" "$active_nvlinks" "$ib_total" "$ib_active" "$ib_bad_counters" "$ib_bad_rate" "$data_path" "$data_path_rc" <<'PY'
import json, socket, sys
(status, codes, reasons, warnings, gpu_count, nvlinks, ib_total, ib_active,
 bad_counters, bad_rate, data_path, data_path_rc) = sys.argv[1:]
payload = {
    "schema": 1,
    "kind": "network",
    "node": socket.gethostname().split(".")[0],
    "status": status,
    "codes": [x for x in codes.split(",") if x],
    "reason": reasons,
    "warnings": [x for x in warnings.split("; ") if x],
    "metrics": {
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
