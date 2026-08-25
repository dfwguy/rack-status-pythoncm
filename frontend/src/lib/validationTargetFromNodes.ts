/*
 * SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 */
import type { NvisNamingSchema } from "../types/naming";

function compressCtNums(nums: number[]): string {
  const parts: string[] = [];
  let i = 0;
  while (i < nums.length) {
    const start = nums[i];
    let end = start;
    while (i + 1 < nums.length && nums[i + 1] === end + 1) {
      i++;
      end = nums[i];
    }
    if (start === end) parts.push(String(start).padStart(2, "0"));
    else parts.push(`${String(start).padStart(2, "0")}-${String(end).padStart(2, "0")}`);
    i++;
  }
  return parts.join(",");
}

function trayFromNodeId(
  rackKey: string,
  nodeId: string,
  naming?: NvisNamingSchema,
): number | null {
  const id = nodeId.trim().toLowerCase();
  const joiner = naming?.ct_joiner ?? "-ct";
  if (joiner && naming?.node_id_mode !== "full_hostname") {
    const esc = rackKey.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const re = new RegExp(`^${esc}${joiner.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(\\d+)$`, "i");
    const m = id.match(re);
    if (m) return parseInt(m[1], 10);
  }
  if (id.startsWith(rackKey) && id.length > rackKey.length) {
    const tail = id.slice(rackKey.length);
    const m = tail.match(/^(\d{2,})$/);
    if (m) return parseInt(m[1], 10);
  }
  return null;
}

/**
 * Build a validation/sysinfo target from the selected tray hostnames.
 *
 * Two shapes are produced depending on the site naming scheme:
 *  - Trays that fold into the ``NVIS_*`` ct template are compressed into the compact
 *    ``<rack><joiner>[NN-MM]`` range form (e.g. ``ink50-r05-ct[01-04]``).
 *  - Nodes whose hostnames do not fold into that template fall back to passing
 *    the **real selected hostnames**
 *    through as a comma list, so the backend targets exactly the chosen nodes instead of
 *    falling back to the whole rack.
 *
 * Returns null when nothing belonging to this rack is selected.
 */
export function validationTargetFromNodes(
  rack: string,
  selected: Iterable<string>,
  naming?: NvisNamingSchema,
): string | null {
  const rackCanon = rack.trim();
  const rackKey = rackCanon.toLowerCase();
  const joiner = naming?.ct_joiner ?? "-ct";
  const nums: number[] = [];
  const hostnames: string[] = [];
  for (const id of selected) {
    const n = trayFromNodeId(rackKey, id, naming);
    if (n !== null && !Number.isNaN(n)) {
      nums.push(n);
    } else {
      const t = id.trim();
      if (t && t.toLowerCase().startsWith(rackKey)) hostnames.push(t);
    }
  }
  if (nums.length > 0) {
    nums.sort((a, b) => a - b);
    return `${rackCanon}${joiner}[${compressCtNums(nums)}]`;
  }
  if (hostnames.length > 0) {
    return Array.from(new Set(hostnames)).join(",");
  }
  return null;
}
