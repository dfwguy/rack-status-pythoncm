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
import { describe, expect, it } from "vitest";
import { fleetNodesFromInventory, rackNodesFromInventory } from "./rackNodesFromInventory";
import type { NodeInventoryEntry } from "../types/api";
import type { NvisNamingSchema } from "../types/naming";

// Nscale NC shape: rack U20 holds sys1-gso1-p-phy-gpu19..36-su19; BCM places them at rack units and
// the server numbers trays by natural hostname order.
const inv: NodeInventoryEntry[] = [
  { id: "sys1-gso1-p-phy-gpu20-su19", rack: "U20", rack_unit: "12", tray: 2, status: "up", ip: "10.10.171.243" },
  { id: "sys1-gso1-p-phy-gpu36-su19", rack: "U20", rack_unit: "37", tray: 18, status: "down", ip: "" },
  { id: "sys1-gso1-p-phy-gpu19-su19", rack: "U20", rack_unit: "11", tray: 1, status: "up", ip: "10.10.171.244" },
  { id: "sys1-gso1-p-phy-gpu1-su19", rack: "U19", rack_unit: "11", tray: 1, status: "up", ip: "10.10.171.28" },
  { id: "appserver-nc", rack: null, status: "up", ip: "" },
];

describe("rackNodesFromInventory", () => {
  it("takes the rack's real hosts from the inventory, in tray order", () => {
    const nodes = rackNodesFromInventory("U20", inv)!;
    expect(nodes.map((n) => n.id)).toEqual([
      "sys1-gso1-p-phy-gpu19-su19",
      "sys1-gso1-p-phy-gpu20-su19",
      "sys1-gso1-p-phy-gpu36-su19",
    ]);
    expect(nodes.every((n) => n.hostname === n.id)).toBe(true);
    expect(nodes.map((n) => n.status)).toEqual(["up", "up", "down"]);
  });

  it("labels each node with its server-numbered tray and keeps the placement for hover", () => {
    const nodes = rackNodesFromInventory("U20", inv)!;
    expect(nodes.map((n) => n.label)).toEqual(["U20-ct01", "U20-ct02", "U20-ct18"]);
    expect(nodes.map((n) => n.placement)).toEqual(["U20:11", "U20:12", "U20:37"]);
  });

  it("uses the cluster's tray joiner", () => {
    const naming: NvisNamingSchema = {
      rack_id: "[A-Z][\\d+]",
      ct_id: "[A-Z][\\d+]-t[\\d+]",
      nvsw_id: "",
      ct_joiner: "-t",
      node_id_mode: "rack_ct_suffix",
    };
    const nodes = rackNodesFromInventory("U20", inv, {}, naming)!;
    expect(nodes[0].label).toBe("U20-t01");
  });

  it("keeps ids as hostnames, never a synthesised tray name", () => {
    const nodes = rackNodesFromInventory("u20", inv)!;
    expect(nodes.some((n) => /-ct\d/.test(n.id))).toBe(false);
  });

  it("fills a missing BMC address from physical_rf0", () => {
    const nodes = rackNodesFromInventory("U20", inv, { "SYS1-GSO1-P-PHY-GPU36-SU19": "10.10.171.227" })!;
    expect(nodes.find((n) => n.id.endsWith("gpu36-su19"))?.ip).toBe("10.10.171.227");
    expect(nodes.find((n) => n.id.endsWith("gpu19-su19"))?.ip).toBe("10.10.171.244");
  });

  it("falls back to the hostname label when the server sent no tray", () => {
    const nodes = rackNodesFromInventory("p5-r01", [
      { id: "p5-r01-ct02", rack: "p5-r01", status: "up", ip: "" },
      { id: "p5-r01-ct01", rack: "p5-r01", status: "up", ip: "" },
    ])!;
    expect(nodes.map((n) => [n.label, n.hostname, n.placement])).toEqual([
      ["p5-r01-ct01", "p5-r01-ct01", undefined],
      ["p5-r01-ct02", "p5-r01-ct02", undefined],
    ]);
  });

  it("returns null without inventory or members, so the caller keeps the legacy path", () => {
    expect(rackNodesFromInventory("U20", undefined)).toBeNull();
    expect(rackNodesFromInventory("U20", null)).toBeNull();
    expect(rackNodesFromInventory("Z9", inv)).toBeNull();
  });
});

describe("fleetNodesFromInventory", () => {
  it("returns every node, racks in natural order, trays within, unplaced last", () => {
    const fleet = fleetNodesFromInventory([
      ...inv,
      { id: "sys1-gso1-p-phy-gpu2-su19", rack: "U2", rack_unit: "11", tray: 1, status: "up", ip: "" },
    ])!;
    expect(fleet.map((n) => [n.rack, n.id])).toEqual([
      ["U2", "sys1-gso1-p-phy-gpu2-su19"],
      ["U19", "sys1-gso1-p-phy-gpu1-su19"],
      ["U20", "sys1-gso1-p-phy-gpu19-su19"],
      ["U20", "sys1-gso1-p-phy-gpu20-su19"],
      ["U20", "sys1-gso1-p-phy-gpu36-su19"],
      [null, "appserver-nc"],
    ]);
    expect(fleet.find((n) => n.id.endsWith("gpu36-su19"))).toMatchObject({ tray: 18, label: "U20-ct18", status: "down" });
    expect(fleet.find((n) => n.id === "appserver-nc")).toMatchObject({ tray: undefined, label: "appserver-nc" });
  });

  it("returns null without an inventory so the caller keeps the legacy path", () => {
    expect(fleetNodesFromInventory(undefined)).toBeNull();
    expect(fleetNodesFromInventory(null)).toBeNull();
  });
});
