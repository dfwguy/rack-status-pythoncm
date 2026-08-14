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
# fabric_probe.sh — emit this node's fabric (NIC) classification as JSON for the NVIS UI.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Runs the shared classifier (lib/fabric.inc.sh) and prints a single JSON object:
#   { host, link, rate_gbps, compute_rails, gpus, nccl_ib_hca, ucx_net_devices,
#     ns_ifname, expect_rails, expect_rate, discrepancies:[...] }
# Expected values come from NVIS_FABRIC_EXPECT_RAILS / NVIS_FABRIC_EXPECT_RATE
# (injected by the app from the active platform's fabric profile). Intended to be
# run on a compute node (directly, via ssh, or via srun).
set -uo pipefail
_D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${_D}/lib/fabric.inc.sh"

# Optional arg: a compute node to probe over ssh (the head has no compute NICs).
# With no arg, probe the local node's sysfs.
_NODE="${1:-}"
if [[ -n "${_NODE}" ]]; then
  _scan="$(nvis_fabric_scan_node "${_NODE}")"
else
  _scan="$(nvis_fabric_scan)"
fi
if [[ "${_scan}" != *NVIS_FAB_IB_HCA=* ]]; then
  printf '{"ok":false,"error":"fabric scan returned no result%s"}\n' "${_NODE:+ for node ${_NODE}}"
  exit 0
fi
# shellcheck disable=SC2046
eval "${_scan}"
gpus="${NVIS_FAB_GPUS:-$(_nvis_fabric_gpu_count)}"

disc=()
if [[ -n "${NVIS_FABRIC_EXPECT_RAILS:-}" && "${NVIS_FAB_RAILS:-0}" != "${NVIS_FABRIC_EXPECT_RAILS}" ]]; then
  disc+=("\"expected ${NVIS_FABRIC_EXPECT_RAILS} compute rails, found ${NVIS_FAB_RAILS:-0}\"")
fi
if [[ -n "${NVIS_FABRIC_EXPECT_RATE:-}" && "${NVIS_FAB_RATE:-0}" != "${NVIS_FABRIC_EXPECT_RATE}" ]]; then
  disc+=("\"expected ${NVIS_FABRIC_EXPECT_RATE}G per rail, found ${NVIS_FAB_RATE:-0}G\"")
fi
disc_json="$(IFS=,; echo "${disc[*]:-}")"

# host = the probed node when we scanned remotely, else this node.
_host="${_NODE:-$(hostname 2>/dev/null)}"
printf '{"host":"%s","link":"%s","rate_gbps":%s,"compute_rails":%s,"gpus":%s,' \
  "${_host}" "${NVIS_FAB_LINK:-}" "${NVIS_FAB_RATE:-0}" "${NVIS_FAB_RAILS:-0}" "${gpus:-0}"
printf '"nccl_ib_hca":"%s","ucx_net_devices":"%s","ns_ifname":"%s",' \
  "${NVIS_FAB_IB_HCA:-}" "${NVIS_FAB_UCX_NET_DEVICES:-}" "${NVIS_FAB_NS_IFNAME:-}"
printf '"expect_rails":"%s","expect_rate":"%s","discrepancies":[%s]}\n' \
  "${NVIS_FABRIC_EXPECT_RAILS:-}" "${NVIS_FABRIC_EXPECT_RATE:-}" "${disc_json}"
