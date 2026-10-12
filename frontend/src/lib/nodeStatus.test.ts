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
import type { NodeInventoryEntry, SchedulerNodeStatus } from "../types/api";
import {
  nodesInTrayOrder,
  SCHEDULER_STATUS,
  SCHEDULER_STATUS_ORDER,
  schedulerRackSummary,
  sourceName,
} from "./nodeStatus";

function node(id: string, status?: SchedulerNodeStatus, tray?: number): NodeInventoryEntry {
  return { id, rack: "U2", ip: "", status: "up", scheduler_status: status, tray };
}

describe("schedulerRackSummary", () => {
  it("counts every node and paints the rack by its majority", () => {
    const s = schedulerRackSummary([node("a", "running"), node("b", "running"), node("c", "idle")]);
    expect(s.total).toBe(3);
    expect(s.counts.running).toBe(2);
    expect(s.counts.idle).toBe(1);
    expect(s.dominant).toBe("running");
  });

  it("breaks a tie toward the more serious status", () => {
    // NC's U2 today: 9 running a burn-in, 9 drained by a health check. That is a rack to look at.
    const nodes = [
      ...Array.from({ length: 9 }, (_, i) => node(`r${i}`, "running")),
      ...Array.from({ length: 9 }, (_, i) => node(`d${i}`, "drained")),
    ];
    expect(schedulerRackSummary(nodes).dominant).toBe("drained");
  });

  it("reads a node an older server sent without a status as unknown", () => {
    const s = schedulerRackSummary([node("a"), node("b", "idle"), node("c")]);
    expect(s.counts.unknown).toBe(2);
    expect(s.dominant).toBe("unknown");
  });

  it("says unknown for a rack with no nodes", () => {
    expect(schedulerRackSummary([]).dominant).toBe("unknown");
  });
});

describe("nodesInTrayOrder", () => {
  it("orders by tray, unplaced nodes last, ties by name", () => {
    const ordered = nodesInTrayOrder([node("z", "idle"), node("b", "idle", 2), node("a", "idle", 1), node("y", "idle")]);
    expect(ordered.map((n) => n.id)).toEqual(["a", "b", "y", "z"]);
  });
});

describe("sourceName", () => {
  it("names the sources the server reports, and passes through ones it does not know", () => {
    const sources = { orchestrator: "bcm", scheduler: "slurm" };
    expect(sourceName(sources, "orchestrator")).toBe("BCM");
    expect(sourceName(sources, "scheduler")).toBe("Slurm");
    expect(sourceName({ orchestrator: "maas", scheduler: "lsf" }, "orchestrator")).toBe("MAAS");
    expect(sourceName({ orchestrator: "maas", scheduler: "lsf" }, "scheduler")).toBe("lsf");
    expect(sourceName(undefined, "scheduler")).toBe("");
  });
});

describe("SCHEDULER_STATUS", () => {
  it("has a distinct look for every status in the server's vocabulary", () => {
    // Seven states, seven colours: two that looked alike would be one state to the eye.
    const looks = SCHEDULER_STATUS_ORDER.map((s) => SCHEDULER_STATUS[s].background);
    expect(new Set(looks).size).toBe(SCHEDULER_STATUS_ORDER.length);
    // Design tokens only -- a hex here ships the wrong colour in the other theme.
    for (const look of looks) expect(look).not.toMatch(/#[0-9a-f]{3,8}\b/i);
  });
});
