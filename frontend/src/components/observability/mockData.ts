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
 * Shared types for the scope-driven observability views (Metrics / Logs / Events).
 *
 * HISTORY: this module used to export a synthetic 3-rack cluster ("GB300 NVL72 —
 * site A", nodes ct03-01 … ct07-06) plus generated metric / log / event series.
 * ALL of that fabricated data has been removed.
 *
 * The scope topology is now **LIVE**: `ScopeExplorer` builds the cluster → rack →
 * tray tree from `GET /api/status` (rack rows + `ct_down_unk` + `physical_rf0` +
 * `naming`) via the same `parseNodesFromRack` path the Rack Detail view uses.
 * Verified against the live backend on 2026-07-28: 32 racks (h01-h08, h15-h22,
 * j01-j08, j15-j22) x 18 trays = 576 hosts named `ink50-dh01-h01-11`, of which
 * 282 were UP. Trays that are not UP render disabled and unselectable.
 *
 * What remains here is types and pure helpers only — no data. The file name is
 * kept so the four observability views' import paths stay stable.
 */

// ---- Topology (cluster → rack → node) --------------------------------------

export interface ScopeNode {
  /** Host id, also used as the selection key and to match log/event hosts. */
  id: string;
  label: string;
  /** Unreachable / powered-off nodes render disabled and cannot be selected. */
  available: boolean;
  /**
   * Live tray state from `ct_down_unk` (`GET /api/status`). Optional so older
   * callers that build a `ScopeNode` by hand keep compiling. Only "up" trays are
   * ever `available`.
   */
  status?: "up" | "down" | "inst" | "unknown";
  /** RedFish / BMC address from `physical_rf0`, when the backend reports one. */
  ip?: string;
}

export interface ScopeRack {
  id: string;
  label: string;
  nodes: ScopeNode[];
}

export interface ScopeCluster {
  id: string;
  label: string;
  racks: ScopeRack[];
}

/** All selectable (available) host ids across the topology. */
export function allAvailableHostIds(topology: ScopeCluster): string[] {
  return topology.racks.flatMap((r) => r.nodes.filter((n) => n.available).map((n) => n.id));
}

/** Human summary of the current selection, e.g. "Rack 07 · 6 nodes" or "9 nodes across 2 racks". */
export function selectionSummary(topology: ScopeCluster, selected: Set<string>): string {
  const count = selected.size;
  if (count === 0) return "No nodes selected";
  const racksTouched = topology.racks.filter((r) => r.nodes.some((n) => selected.has(n.id)));
  const fullRacks = racksTouched.filter((r) => {
    const avail = r.nodes.filter((n) => n.available);
    return avail.length > 0 && avail.every((n) => selected.has(n.id));
  });
  if (racksTouched.length === 1) {
    const r = racksTouched[0];
    const label = fullRacks.length === 1 ? r.label : `${r.label} (partial)`;
    return `${label} · ${count} node${count === 1 ? "" : "s"}`;
  }
  return `${count} nodes across ${racksTouched.length} racks`;
}

// ---- Logs -------------------------------------------------------------------

export type LogLevel = "info" | "warn" | "error";

export interface LogLine {
  ts: string;
  level: LogLevel;
  host: string;
  msg: string;
}

// ---- Events -----------------------------------------------------------------

export type EventKind = "drain" | "job" | "power" | "lock" | "alert";

/** Presentation only (tag colour + label) — safe to keep, carries no data. */
export const EVENT_META: Record<EventKind, { color: "green" | "red" | "yellow" | "blue"; label: string }> = {
  drain: { color: "yellow", label: "Drain" },
  job: { color: "green", label: "Job" },
  power: { color: "blue", label: "Power" },
  lock: { color: "blue", label: "Lock" },
  alert: { color: "red", label: "Alert" },
};

export interface EventItem {
  ts: string;
  kind: EventKind;
  host: string;
  text: string;
}
