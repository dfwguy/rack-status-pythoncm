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
  const rfByHost = new Map(Object.entries(physicalRf0).map(([h, ip]) => [h.toLowerCase(), ip]));
  const joiner = naming?.ct_joiner ?? "-ct";
  const trayNum = (n: NodeInventoryEntry) =>
    typeof n.tray === "number" && n.tray > 0 ? n.tray : Number.POSITIVE_INFINITY;
  return [...members]
    .sort((a, b) => trayNum(a) - trayNum(b) || a.id.localeCompare(b.id, undefined, { numeric: true }))
    .map((n) => {
      const rackId = n.rack ?? rack;
      return {
        id: n.id,
        hostname: n.id,
        label: typeof n.tray === "number" && n.tray > 0 ? `${rackId}${joiner}${String(n.tray).padStart(2, "0")}` : n.id,
        placement: n.rack_unit ? `${rackId}:${n.rack_unit}` : undefined,
        status: n.status,
        ip: n.ip || rfByHost.get(n.id.toLowerCase()) || "",
      };
    });
}
