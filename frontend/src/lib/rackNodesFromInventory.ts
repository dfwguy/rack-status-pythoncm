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
 * A rack's members, taken from the node inventory the server already resolved.
 *
 * Membership is the server's call, made by the cluster's rack-mapping mode and naming templates
 * (``direct`` parses the rack out of the hostname by template, ``translate`` takes the cluster
 * manager's placement through the rack-id mapping). The browser does not re-derive it: building
 * ``<rack>-ctNN`` names from a template is only right when the hostnames really are those names,
 * and on a translate cluster such as Nscale NC (rack U20 = sys1-gso1-p-phy-gpu19..36-su19) it
 * names hosts that do not exist -- no status, no RedFish IP, and actions aimed at nothing.
 *
 * Returns null when the server sent no inventory or none of it is placed in this rack, so the
 * caller can fall back to the legacy ``ct_down_unk`` expansion for older servers.
 */
import type { NodeInfo } from "../components/rack/NodeSelector";
import type { NodeInventoryEntry } from "../types/api";
import type { NvisNamingSchema } from "../types/naming";

/** Heatmap groups for nodes the inventory leaves without a rack, or does not list at all. */
export const UNPLACED_GROUP = "Unplaced";
export const NOT_IN_INVENTORY_GROUP = "Not in inventory";

/** Label/sort helpers shared by the rack page and the fleet heatmap. */
const trayOf = (n: NodeInventoryEntry): number | undefined =>
  typeof n.tray === "number" && n.tray > 0 ? n.tray : undefined;

function byTrayThenName(a: NodeInventoryEntry, b: NodeInventoryEntry): number {
  const ta = trayOf(a) ?? Number.POSITIVE_INFINITY;
  const tb = trayOf(b) ?? Number.POSITIVE_INFINITY;
  return ta - tb || a.id.localeCompare(b.id, undefined, { numeric: true });
}

function toNodeInfo(
  n: NodeInventoryEntry,
  rackId: string,
  joiner: string,
  rfByHost: Map<string, string>,
): NodeInfo {
  const tray = trayOf(n);
  return {
    id: n.id,
    hostname: n.id,
    label: tray !== undefined && rackId ? `${rackId}${joiner}${String(tray).padStart(2, "0")}` : n.id,
    placement: n.rack_unit && rackId ? `${rackId}:${n.rack_unit}` : undefined,
    tray,
    status: n.status,
    ip: n.ip || rfByHost.get(n.id.toLowerCase()) || "",
  };
}

function rfMap(physicalRf0: Record<string, string>): Map<string, string> {
  return new Map(Object.entries(physicalRf0).map(([h, ip]) => [h.toLowerCase(), ip]));
}

/**
 * ``tray`` is the server's number (natural hostname order within the rack); the label renders it
 * with the cluster's own tray joiner, as the dashboard names trays (``U20`` + ``-ct`` + ``01``).
 */
export function rackNodesFromInventory(
  rack: string,
  inventory: NodeInventoryEntry[] | null | undefined,
  physicalRf0: Record<string, string> = {},
  naming?: NvisNamingSchema,
): NodeInfo[] | null {
  if (!Array.isArray(inventory)) return null;
  const rackKey = rack.trim().toLowerCase();
  const members = inventory.filter((n) => (n.rack ?? "").trim().toLowerCase() === rackKey);
  if (members.length === 0) return null;
  const rfByHost = rfMap(physicalRf0);
  const joiner = naming?.ct_joiner ?? "-ct";
  return [...members].sort(byTrayThenName).map((n) => toNodeInfo(n, n.rack ?? rack, joiner, rfByHost));
}

/**
 * The whole fleet from the server's inventory, for views that draw every node (the Analytics
 * heatmap). Racks in natural order (``Q2`` before ``Q10``), trays in order within each; unplaced
 * nodes last. Returns null when the server sent no inventory, so the caller can fall back.
 */
export function fleetNodesFromInventory(
  inventory: NodeInventoryEntry[] | null | undefined,
  physicalRf0: Record<string, string> = {},
  naming?: NvisNamingSchema,
): Array<NodeInfo & { rack: string | null }> | null {
  if (!Array.isArray(inventory)) return null;
  const rfByHost = rfMap(physicalRf0);
  const joiner = naming?.ct_joiner ?? "-ct";
  const rackKey = (n: NodeInventoryEntry) => (n.rack ?? "").trim();
  return [...inventory]
    .sort((a, b) => {
      const ra = rackKey(a);
      const rb = rackKey(b);
      if (!ra !== !rb) return ra ? -1 : 1;
      return ra.localeCompare(rb, undefined, { numeric: true }) || byTrayThenName(a, b);
    })
    .map((n) => ({ ...toNodeInfo(n, rackKey(n), joiner, rfByHost), rack: rackKey(n) || null }));
}
