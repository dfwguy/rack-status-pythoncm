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
 * Whether a rack may join a multirack launch: validation done, and every node in it schedulable.
 *
 * The nodes are the rack's members in the server's node inventory, placed by the cluster's
 * rack-mapping mode and naming templates, and each is judged by its own Slurm state. The rack row's
 * ``sinfo`` and ``ct_down_unk`` strings cannot carry that judgement everywhere: on a cluster whose
 * hostnames do not name the rack (Nscale NC's sys1-gso1-p-phy-gpu19-su19 sits in U20) the row's
 * ``sinfo`` cell is the cluster manager's up/down tally (``up=18``), never a Slurm state, and
 * ``ct_down_unk`` is ``-`` because no hostname fits the tray template -- so every validated rack read
 * ineligible. The string checks remain only for a server that sends no inventory for the rack.
 */
import type { NodeInventoryEntry, RackRow } from "../types/api";

export interface RackEligibility {
  eligible: boolean;
  reason: string;
}

/** Slurm compact states (``sinfo %t``) a launch may take: idle, or allocated to a yieldable job. */
const SCHEDULABLE = new Set(["idle", "alloc", "allocated"]);

/** ``alloc+drain`` -> ``["alloc", "drain"]``; the server joins a node's per-partition states with ``+``. */
function slurmStates(schedulerState: string | undefined): string[] {
  return (schedulerState ?? "")
    .toLowerCase()
    .split("+")
    .map((s) => s.trim().replace(/[*~$#:!%@^-]+$/, ""))
    .filter(Boolean);
}

/** Why a node cannot be scheduled, or "" when it can. */
function nodeBlocker(node: NodeInventoryEntry): string {
  if (node.status === "down") return "down";
  const states = slurmStates(node.scheduler_state);
  if (states.length === 0) return "no slurm state";
  const bad = states.filter((s) => !SCHEDULABLE.has(s));
  return bad.length ? bad.join("+") : "";
}

function legacyEligibility(row: RackRow): RackEligibility {
  const sinfoStates = row.sinfo.split(",").map((s) => s.trim().split("=")[0].toLowerCase()).filter(Boolean);
  if (sinfoStates.length === 0) return { eligible: false, reason: "no sinfo data" };
  if (sinfoStates.some((s) => !SCHEDULABLE.has(s))) {
    return { eligible: false, reason: `sinfo ${row.sinfo}` };
  }
  if (row.ct_down_unk.includes("DN:")) return { eligible: false, reason: "has DN nodes" };
  return { eligible: true, reason: "" };
}

/** Index the inventory by rack once, so a table of hundreds of racks does not rescan it per row. */
export function inventoryByRack(
  inventory: NodeInventoryEntry[] | null | undefined,
): Map<string, NodeInventoryEntry[]> | null {
  if (!Array.isArray(inventory)) return null;
  const byRack = new Map<string, NodeInventoryEntry[]>();
  for (const node of inventory) {
    const key = (node.rack ?? "").trim().toLowerCase();
    if (!key) continue;
    const members = byRack.get(key);
    if (members) members.push(node);
    else byRack.set(key, [node]);
  }
  return byRack;
}

export function multirackEligibility(
  row: RackRow,
  byRack: Map<string, NodeInventoryEntry[]> | null,
): RackEligibility {
  if (row.nemo !== "done") return { eligible: false, reason: "need validation done" };
  const members = byRack?.get(row.rack.trim().toLowerCase());
  if (!members || members.length === 0) return legacyEligibility(row);
  const blocked = new Map<string, number>();
  for (const node of members) {
    const why = nodeBlocker(node);
    if (why) blocked.set(why, (blocked.get(why) ?? 0) + 1);
  }
  if (blocked.size === 0) return { eligible: true, reason: "" };
  const detail = [...blocked.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([why, n]) => `${why}=${n}`)
    .join(", ");
  return { eligible: false, reason: `nodes not schedulable (${detail})` };
}
