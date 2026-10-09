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

export function rackNodesFromInventory(
  rack: string,
  inventory: NodeInventoryEntry[] | null | undefined,
  physicalRf0: Record<string, string> = {},
): NodeInfo[] | null {
  if (!Array.isArray(inventory)) return null;
  const rackKey = rack.trim().toLowerCase();
  const members = inventory.filter((n) => (n.rack ?? "").trim().toLowerCase() === rackKey);
  if (members.length === 0) return null;
  const rfByHost = new Map(Object.entries(physicalRf0).map(([h, ip]) => [h.toLowerCase(), ip]));
  const unitNum = (n: NodeInventoryEntry) => {
    const v = Number.parseInt(n.rack_unit ?? "", 10);
    return Number.isNaN(v) ? Number.POSITIVE_INFINITY : v;
  };
  return [...members]
    .sort((a, b) => unitNum(a) - unitNum(b) || a.id.localeCompare(b.id, undefined, { numeric: true }))
    .map((n) => ({
      id: n.id,
      hostname: n.id,
      label: n.rack_unit ? `${n.rack ?? rack}:${n.rack_unit}` : n.id,
      status: n.status,
      ip: n.ip || rfByHost.get(n.id.toLowerCase()) || "",
    }));
}
