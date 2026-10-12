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
 * A node seen two ways: by the orchestrator that owns the hardware, and by the scheduler that
 * hands it work.
 *
 * The two disagree in the cases an operator most needs to see. On Nscale NC most of the fleet is
 * up to BCM -- powered, reachable, healthy -- and drained to Slurm, so a floor drawn only from BCM
 * reads green while nothing can be scheduled. The server sends both halves on every node record
 * (``status`` and ``scheduler_status``) and names who answered for each (``node_sources``); this
 * module owns how the floor view colours and counts them.
 */
import type { NodeInventoryEntry, NodeSources, SchedulerNodeStatus } from "../types/api";

export type FloorStatusMode = "orchestrator" | "scheduler";

export interface StatusStyle {
  label: string;
  /** A CSS ``background`` value: a colour token, or a pattern where one colour cannot say it. */
  background: string;
}

export const ORCHESTRATOR_STATUS: Record<NodeInventoryEntry["status"], StatusStyle> = {
  up: { label: "Up", background: "var(--color-green-500)" },
  inst: { label: "Installing", background: "var(--color-teal-500)" },
  down: { label: "Down", background: "var(--color-red-500)" },
  unknown: { label: "No data", background: "var(--color-gray-400)" },
};

export const SCHEDULER_STATUS: Record<SchedulerNodeStatus, StatusStyle> = {
  idle: { label: "Idle", background: "var(--color-green-500)" },
  running: { label: "Running", background: "var(--color-blue-500)" },
  reserved: { label: "Reserved", background: "var(--color-purple-500)" },
  // Still running work, and drained once it ends: both colours at once, so it reads as either.
  draining: {
    label: "Draining",
    background:
      "repeating-linear-gradient(135deg, var(--color-blue-500) 0 3px, var(--color-yellow-500) 3px 6px)",
  },
  drained: { label: "Drained", background: "var(--color-yellow-500)" },
  down: { label: "Down", background: "var(--color-red-500)" },
  unknown: { label: "Unknown", background: "var(--color-gray-400)" },
};

/** Legend and count order: from "can take work" to "cannot say", as the server's vocabulary is. */
export const SCHEDULER_STATUS_ORDER: SchedulerNodeStatus[] = [
  "idle",
  "running",
  "reserved",
  "draining",
  "drained",
  "down",
  "unknown",
];

const ORCHESTRATOR_NAME: Record<string, string> = { bcm: "BCM", maas: "MAAS" };
const SCHEDULER_NAME: Record<string, string> = { slurm: "Slurm", pythoncm: "pythoncm" };

/** "BCM" / "Slurm" for a source id; the id itself for one this table does not know yet. */
export function sourceName(sources: NodeSources | undefined, mode: FloorStatusMode): string {
  const id = (mode === "orchestrator" ? sources?.orchestrator : sources?.scheduler) ?? "";
  const names = mode === "orchestrator" ? ORCHESTRATOR_NAME : SCHEDULER_NAME;
  return names[id.toLowerCase()] ?? id;
}

/** A node's scheduler status; a server too old to send one says nothing, so "unknown". */
export function schedulerStatusOf(node: NodeInventoryEntry): SchedulerNodeStatus {
  return node.scheduler_status ?? "unknown";
}

export interface SchedulerRackSummary {
  total: number;
  counts: Record<SchedulerNodeStatus, number>;
  /**
   * The status most of the rack is in -- what the tile's edge is painted. A tie goes to the later,
   * more serious status in ``SCHEDULER_STATUS_ORDER``: half drained and half idle is a rack to look
   * at, not an idle one.
   */
  dominant: SchedulerNodeStatus;
}

export function schedulerRackSummary(nodes: NodeInventoryEntry[]): SchedulerRackSummary {
  const counts = Object.fromEntries(SCHEDULER_STATUS_ORDER.map((s) => [s, 0])) as Record<
    SchedulerNodeStatus,
    number
  >;
  for (const node of nodes) counts[schedulerStatusOf(node)] += 1;
  let dominant: SchedulerNodeStatus = "unknown";
  let best = 0;
  for (const status of SCHEDULER_STATUS_ORDER) {
    if (counts[status] > 0 && counts[status] >= best) {
      best = counts[status];
      dominant = status;
    }
  }
  return { total: nodes.length, counts, dominant };
}

/** A rack's nodes in tray order -- the server numbers trays so every view draws them alike. */
export function nodesInTrayOrder(nodes: NodeInventoryEntry[]): NodeInventoryEntry[] {
  return [...nodes].sort(
    (a, b) => (a.tray ?? Number.MAX_SAFE_INTEGER) - (b.tray ?? Number.MAX_SAFE_INTEGER) || a.id.localeCompare(b.id),
  );
}
