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
# GB300 per-node deep hardware audit.  One machine-readable result is always
# emitted; confirmed failures exit 2 so the workflow fails and drains the node.
set -uo pipefail

expected_gpus="${RACK_AUDIT_EXPECTED_GPUS:-4}"
max_temp_c="${RACK_AUDIT_MAX_GPU_TEMP_C:-85}"
codes=()
reasons=()
warnings=()

fail() { codes+=("$1"); reasons+=("$2"); }
warn() { warnings+=("$1: $2"); }

gpu_count=$(nvidia-smi -L 2>/dev/null | grep -c '^GPU ' || true)
if [[ "$gpu_count" -ne "$expected_gpus" ]]; then
  fail gpu_count "expected ${expected_gpus} GPUs but nvidia-smi enumerated ${gpu_count}"
fi

temps=$(nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null || true)
if [[ -z "$temps" ]]; then
  warn gpu_temperature "temperature query unavailable"
else
  while IFS= read -r temp; do
    temp=${temp//[!0-9]/}
    if [[ -n "$temp" && "$temp" -gt "$max_temp_c" ]]; then
      fail gpu_temperature "GPU temperature ${temp}C exceeds ${max_temp_c}C"
    fi
  done <<< "$temps"
fi

# Match the state exactly.  "Not Active" must never be counted as a slowdown.
perf=$(nvidia-smi -q -d PERFORMANCE 2>/dev/null || true)
if grep -Eiq 'HW Thermal Slowdown[^:]*:[[:space:]]*Active[[:space:]]*$' <<< "$perf"; then
  fail thermal_slowdown "nvidia-smi reports HW Thermal Slowdown Active"
fi

ecc=$(nvidia-smi --query-gpu=ecc.errors.uncorrected.volatile.total,ecc.errors.uncorrected.aggregate.total --format=csv,noheader,nounits 2>/dev/null || true)
if [[ -z "$ecc" ]]; then
  warn ecc "uncorrectable ECC query unsupported or unavailable"
elif awk -F, '{gsub(/ /,""); if (($1 ~ /^[0-9]+$/ && $1>0) || ($2 ~ /^[0-9]+$/ && $2>0)) bad=1} END{exit !bad}' <<< "$ecc"; then
  fail uncorrectable_ecc "one or more GPUs report non-zero uncorrectable ECC"
fi

query=$(nvidia-smi -q 2>/dev/null || true)
fabric_completed=$(grep -Eic 'State[[:space:]]*:[[:space:]]*Completed[[:space:]]*$' <<< "$query" || true)
fabric_success=$(grep -Eic 'Status[[:space:]]*:[[:space:]]*Success[[:space:]]*$' <<< "$query" || true)
if [[ "$gpu_count" -gt 0 && ( "$fabric_completed" -lt "$gpu_count" || "$fabric_success" -lt "$gpu_count" ) ]]; then
  fail gpu_fabric "GPU fabric is not Completed/Success on all ${gpu_count} GPUs"
fi
if grep -Eiq 'Recovery Action[[:space:]]*:[[:space:]]*(Reset|Reboot|Drain|Repair|RMA|[1-9][0-9]*)[[:space:]]*$' <<< "$query"; then
  fail gpu_recovery_action "nvidia-smi reports a pending GPU recovery action"
fi

kernel=$(journalctl -k -b --no-pager 2>/dev/null || dmesg 2>/dev/null || true)
xid_count=$(grep -Eic 'NVRM: Xid|Xid \([A-Za-z0-9_-]+:[0-9]+' <<< "$kernel" || true)
pcie_fatal_count=$(grep -Eic 'AER:.*(Uncorrected|Fatal)|DPC:.*(containment|recovery failed)|PCIe Bus Error: severity=(Uncorrected|Fatal)' <<< "$kernel" || true)
edac_count=$(grep -Eic 'EDAC.*(uncorrect|fatal)|MCE:.*Hardware Error' <<< "$kernel" || true)
[[ "$xid_count" -gt 0 ]] && fail kernel_xid "current boot contains ${xid_count} GPU Xid event(s)"
[[ "$pcie_fatal_count" -gt 0 ]] && fail pcie_fatal "current boot contains ${pcie_fatal_count} uncorrected/fatal PCIe or DPC event(s)"
[[ "$edac_count" -gt 0 ]] && fail host_ecc "current boot contains ${edac_count} uncorrected EDAC/MCE event(s)"

for service in slurmd nvidia-imex; do
  if ! systemctl is-active --quiet "$service" 2>/dev/null; then
    fail "service_${service}" "${service} is not active"
  fi
done
if systemctl list-unit-files CMDaemon.service >/dev/null 2>&1 && ! systemctl is-active --quiet CMDaemon; then
  fail service_cmdaemon "CMDaemon is installed but not active"
fi

# Only a *configured* md0 must be active. Guard on md0 actually appearing in /proc/mdstat, the way
# the /raid check below guards on the directory existing. The md driver is loaded on every node, so
# `-e /proc/mdstat` is always true -- the old guard failed the whole fleet of nodes that run no md
# RAID at all (GB300 boots nvme directly: mdstat reads `unused devices: <none>`). An md0 that is
# assembled-but-degraded still prints an `md0 :` line without `active`, so this still catches it.
if grep -qE '^md0[[:space:]]*:' /proc/mdstat 2>/dev/null; then
  # State is the field after "md0 :" -- match it whole. A plain /active/ regex also matches
  # "inactive" (it contains "active"), which would score a degraded array as healthy.
  if ! awk '$1=="md0" && $3=="active" {ok=1} END {exit !ok}' /proc/mdstat 2>/dev/null; then
    fail storage_md0 "md0 is not active"
  fi
fi
if [[ -d /raid ]] && ! findmnt -rn /raid >/dev/null 2>&1; then
  fail storage_raid_mount "/raid exists but is not mounted"
fi

status=pass
[[ ${#warnings[@]} -gt 0 ]] && status=warning
[[ ${#codes[@]} -gt 0 ]] && status=fail
codes_joined=$(IFS=,; printf '%s' "${codes[*]}")
reasons_joined=$(IFS='; '; printf '%s' "${reasons[*]}")
warnings_joined=$(IFS='; '; printf '%s' "${warnings[*]}")

python3 - "$status" "$codes_joined" "$reasons_joined" "$warnings_joined" "$gpu_count" "$temps" "$xid_count" "$pcie_fatal_count" <<'PY'
import json, socket, sys
status, codes, reasons, warnings, gpu_count, temps, xids, pcie = sys.argv[1:]
payload = {
    "schema": 1,
    "kind": "hardware",
    "node": socket.gethostname().split(".")[0],
    "status": status,
    "codes": [x for x in codes.split(",") if x],
    "reason": reasons,
    "warnings": [x for x in warnings.split("; ") if x],
    "metrics": {
        "gpu_count": int(gpu_count or 0),
        "gpu_temperatures_c": [int(x.strip()) for x in temps.splitlines() if x.strip().isdigit()],
        "kernel_xids": int(xids or 0),
        "pcie_fatal_events": int(pcie or 0),
    },
}
print("NVIS_DEEP_AUDIT_JSON=" + json.dumps(payload, separators=(",", ":")))
PY

[[ "$status" == fail ]] && exit 2
exit 0
