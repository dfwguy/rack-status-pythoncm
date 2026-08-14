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
/**
 * Expand a rack row's `ct_down_unk` string into per-node records.
 *
 * In lib/ rather than beside the NodeSelector component: six modules call it, and a file that
 * exports both a component and a helper costs React Refresh a full page reload on every edit.
 */
import type { NvisNamingSchema } from "../types/naming";
import type { NodeInfo } from "../components/rack/NodeSelector";

function nodeIdForTray(
  rack: string,
  num: number,
  physicalRf0: Record<string, string>,
  naming?: NvisNamingSchema,
): string {
  const pad = String(num).padStart(2, "0");
  const joiner = naming?.ct_joiner ?? "-ct";
  const rk = rack.trim();
  const rkKey = rk.toLowerCase();
  if (naming?.node_id_mode === "full_hostname") {
    const suffix = pad;
    const hit = Object.keys(physicalRf0).find(
      (h) => h.toLowerCase().startsWith(rkKey) && h.toLowerCase().endsWith(suffix),
    );
    if (hit) return hit;
    return `${rk}${suffix}`;
  }
  return `${rk}${joiner}${pad}`;
}

function expandRange(inner: string): number[] {
  const nums: number[] = [];
  for (const part of inner.split(",")) {
    const trimmed = part.trim();
    if (trimmed.includes("-")) {
      const [startStr, endStr] = trimmed.split("-");
      const start = parseInt(startStr, 10);
      const end = parseInt(endStr, 10);
      if (!isNaN(start) && !isNaN(end)) {
        for (let i = start; i <= end; i++) nums.push(i);
      }
    } else {
      const n = parseInt(trimmed, 10);
      if (!isNaN(n)) nums.push(n);
    }
  }
  return nums;
}

/**
 * Parse the ct_down_unk string (e.g. "DN:p5-r01-ct[01-02];UP:p5-r01-ct[03-08]")
 * and physical_rf0 map into a list of NodeInfo objects.
 */
export function parseNodesFromRack(
  rack: string,
  ctDownUnk: string,
  physicalRf0: Record<string, string>,
  ctCount = 8,
  naming?: NvisNamingSchema,
): NodeInfo[] {
  const nodes: Map<string, NodeInfo> = new Map();
  const rackCanon = rack.trim();
  const rackKey = rackCanon.toLowerCase();
  const joiner = naming?.ct_joiner ?? "-ct";
  const bracketRe = joiner
    ? new RegExp(`${joiner.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\[([^\\]]+)\\]`, "i")
    : /\[([^\]]+)\]/i;

  const rfIp = (id: string): string => {
    if (physicalRf0[id] !== undefined) return physicalRf0[id] || "";
    const hit = Object.keys(physicalRf0).find((k) => k.toLowerCase() === id.toLowerCase());
    return hit ? physicalRf0[hit] || "" : "";
  };

  const segments = ctDownUnk.split(";").map((s) => s.trim()).filter(Boolean);
  for (const seg of segments) {
    let status: NodeInfo["status"] = "unknown";
    if (seg.startsWith("DN:")) status = "down";
    else if (seg.startsWith("UP:")) status = "up";
    else if (seg.startsWith("INST:")) status = "inst";

    const bracketMatch = seg.match(bracketRe);
    if (bracketMatch) {
      const inner = bracketMatch[1];
      for (const num of expandRange(inner)) {
        const id = nodeIdForTray(rackCanon, num, physicalRf0, naming);
        const ip = rfIp(id);
        nodes.set(id.toLowerCase(), { id, status, ip });
      }
    }
  }

  if (naming?.node_id_mode === "full_hostname") {
    for (const host of Object.keys(physicalRf0)) {
      const hKey = host.toLowerCase();
      if (hKey.startsWith(rackKey) && host.length > rackCanon.length && !nodes.has(hKey)) {
        nodes.set(hKey, { id: host, status: "unknown", ip: physicalRf0[host] || "" });
      }
    }
  } else {
    for (let i = 1; i <= ctCount; i++) {
      const id = nodeIdForTray(rackCanon, i, physicalRf0, naming);
      const k = id.toLowerCase();
      if (!nodes.has(k)) {
        const ip = rfIp(id);
        nodes.set(k, { id, status: "unknown", ip });
      }
    }
  }

  return [...nodes.values()].sort((a, b) => a.id.localeCompare(b.id, undefined, { numeric: true }));
}
