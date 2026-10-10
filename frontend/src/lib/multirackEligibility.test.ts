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
import { inventoryByRack, multirackEligibility } from "./multirackEligibility";
import type { NodeInventoryEntry, RackRow } from "../types/api";

// Nscale NC: the hostnames name no rack, BCM places them, and the rack row's sinfo cell is the
// cluster manager's up/down tally rather than a Slurm state.
const ncRow = (rack: string, over: Partial<RackRow> = {}): RackRow => ({
  rack, nemo: "done", last_job: "1", sinfo: "up=18", ct_down_unk: "-", ...over,
});

const ncNode = (n: number, rack: string, over: Partial<NodeInventoryEntry> = {}): NodeInventoryEntry => ({
  id: `sys1-gso1-p-phy-gpu${n}-su19`, rack, status: "up", ip: "", scheduler_state: "idle", ...over,
});

const rackOf = (rack: string, first: number, over: Record<number, Partial<NodeInventoryEntry>> = {}) =>
  Array.from({ length: 18 }, (_, i) => ncNode(first + i, rack, over[first + i]));

describe("multirackEligibility", () => {
  it("finds a validated rack eligible from its placed nodes' Slurm states, whatever the sinfo cell says", () => {
    const byRack = inventoryByRack(rackOf("U20", 19, { 20: { scheduler_state: "alloc" } }));
    expect(multirackEligibility(ncRow("U20"), byRack)).toEqual({ eligible: true, reason: "" });
  });

  it("needs validation done before it looks at nodes", () => {
    const byRack = inventoryByRack(rackOf("U20", 19));
    expect(multirackEligibility(ncRow("U20", { nemo: "pending" }), byRack).reason).toBe("need validation done");
  });

  it("names the nodes that block a rack, by state", () => {
    const byRack = inventoryByRack(rackOf("U20", 19, {
      20: { scheduler_state: "drain" },
      21: { scheduler_state: "alloc+drain" },
      22: { status: "down", scheduler_state: "down" },
      23: { scheduler_state: "mix" },
    }));
    expect(multirackEligibility(ncRow("U20"), byRack)).toEqual({
      eligible: false,
      reason: "nodes not schedulable (drain=2, down=1, mix=1)",
    });
  });

  it("does not take a node with no Slurm state as schedulable", () => {
    const byRack = inventoryByRack(rackOf("U20", 19, { 19: { scheduler_state: "" } }));
    expect(multirackEligibility(ncRow("U20"), byRack).reason).toBe("nodes not schedulable (no slurm state=1)");
  });

  it("judges each rack by its own members only, case-insensitively", () => {
    const byRack = inventoryByRack([
      ...rackOf("U20", 19),
      ...rackOf("U21", 37, { 40: { scheduler_state: "drain" } }),
      { id: "appserver-nc", rack: null, status: "up", ip: "", scheduler_state: "down" },
    ]);
    expect(multirackEligibility(ncRow("u20"), byRack).eligible).toBe(true);
    expect(multirackEligibility(ncRow("U21"), byRack).eligible).toBe(false);
  });

  it("falls back to the row's sinfo and tray strings when the server sends no inventory for the rack", () => {
    const row = (over: Partial<RackRow>) =>
      ({ rack: "ink50-dh01-d02", nemo: "done", last_job: "1", sinfo: "idle=18", ct_down_unk: "-", ...over }) as RackRow;
    expect(multirackEligibility(row({}), null).eligible).toBe(true);
    expect(multirackEligibility(row({ sinfo: "alloc=15,idle=3" }), inventoryByRack([])).eligible).toBe(true);
    expect(multirackEligibility(row({ sinfo: "idle=17,drain=1" }), null).reason).toBe("sinfo idle=17,drain=1");
    expect(multirackEligibility(row({ ct_down_unk: "DN:03" }), null).reason).toBe("has DN nodes");
    expect(multirackEligibility(row({ sinfo: "" }), null).reason).toBe("no sinfo data");
  });
});
