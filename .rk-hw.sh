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
echo "== host =="; hostname -s; uptime -p
echo "== GPU temp/power/ecc =="
nvidia-smi --query-gpu=index,temperature.gpu,power.draw,power.limit,ecc.errors.uncorrected.aggregate.total --format=csv 2>&1 | head
echo "== GPU thermal/throttle/perf-limit detail =="
nvidia-smi -q -d TEMPERATURE,PERFORMANCE 2>/dev/null | grep -iE "Current Temp|Limit|Slowdown|Shutdown|Thermal|Throttle|Clocks Event|: Active|: Not Active" | head -50
echo "== MCE / hardware error / EDAC / PCIe-AER / thermal in dmesg =="
dmesg 2>/dev/null | grep -iE "mce:|hardware error|machine check|edac|uncorrected|corrected error|aer:|pcieport.*error|thermal|throttl|critical temp|over temp" | tail -25
echo "== EDAC memory error summary =="
if command -v ras-mc-ctl >/dev/null 2>&1; then ras-mc-ctl --summary 2>/dev/null | head -20; else echo "ras-mc-ctl not present"; fi
echo "== mlx5 health recurrence / errors since boot =="
echo "health_compromised_total=$(dmesg 2>/dev/null | grep -c 'health compromised')"
echo "newest_dmesg_line: $(dmesg -T 2>/dev/null | tail -1)"
dmesg 2>/dev/null | grep -iE "mlx5.*(err|fatal|reset|recover|health|FW)" | tail -8
echo "== NVLink errors (nvidia-smi nvlink) =="
nvidia-smi nvlink -e 2>/dev/null | grep -iE "GPU|Replay|Recovery|CRC" | head -25
echo "== Xid since boot =="; dmesg 2>/dev/null | grep -c "NVRM: Xid"
