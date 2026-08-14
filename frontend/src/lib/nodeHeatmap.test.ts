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
import {
  COMPLETION_FILL,
  COMPLETION_VIEW_ID,
  GAUGE_TRACK,
  STATUS_BORDER,
  STATUS_FILL,
  STATUS_VIEW_ID,
  VERDICT_BORDER,
  VERDICT_BORDER_UNKNOWN,
  borderFor,
  GAUGE_GRADIENT,
  cellFill,
  cellTone,
  chunkNodes,
  compareMetric,
  completionOf,
  groupByRack,
  groupRacksByRow,
  heatmapViews,
  metricOf,
  metricRange,
  orderColumns,
  paintCell,
  rackRowOf,
  shortNodeLabel,
  shortRackLabel,
  statusRank,
  throughputColor,
  toneCounts,
  verdictLabel,
  verdictOf,
  verdictRank,
} from "./nodeHeatmap";
import type { BringupFleet, BringupFleetNode } from "../types/bringup";

function node(name: string, allreduce: string, metric: string | null): BringupFleetNode {
  return {
    node: name,
    slurm_state: "idle",
    jobs: [
      { name: "dcgmi-r1", base: "dcgmi-r1", status: "DONE", reason: "", metric: null, metric_label: null },
      { name: "allreduce", base: "allreduce", status: allreduce, reason: "", metric, metric_label: "busbw GB/s" },
    ],
  };
}

const FLEET: BringupFleet = {
  home: "/home/cmods/bringup",
  columns: ["dcgmi-r1", "allreduce"],
  metric_labels: { allreduce: "busbw GB/s" },
  nodes: [
    node("r01-11", "DONE", "688.68"),
    node("r01-12", "FAIL", "2.28"),
    node("r02-11", "NOT_RUN", null),
  ],
};

describe("heatmapViews", () => {
  it("offers status and completion first, then one view per measurable column", () => {
    const views = heatmapViews(FLEET);
    expect(views.map((v) => v.id)).toEqual([STATUS_VIEW_ID, COMPLETION_VIEW_ID, "allreduce"]);
    // dcgmi-r1 produces no number, so it is not a heatmap view.
    expect(views[2].unit).toBe("busbw GB/s");
    expect(views.map((v) => v.kind)).toEqual(["status", "completion", "metric"]);
  });

  it("still offers the fleet-independent views when there is no data", () => {
    expect(heatmapViews(null).map((v) => v.id)).toEqual([STATUS_VIEW_ID, COMPLETION_VIEW_ID]);
  });
});

describe("completionOf", () => {
  const jobs = (...statuses: string[]) => ({
    node: "n",
    slurm_state: "idle",
    jobs: statuses.map((status, i) => ({
      name: `t${i}`, base: `t${i}`, status, reason: "", metric: null, metric_label: null,
    })),
  });

  it("counts a test as done once it reaches a verdict, pass or fail", () => {
    /** Completion asks "has it been tested", not "did it pass" -- a fail is still an answer. */
    expect(completionOf(jobs("DONE", "PASS", "FAIL"))).toBe("all_done");
  });

  it("is none_run only when nothing has started", () => {
    expect(completionOf(jobs("NOT_RUN", "NOT_RUN"))).toBe("none_run");
    expect(completionOf(jobs("NOT_RUN", ""))).toBe("none_run");
  });

  it("treats an in-flight test as partial, never as done", () => {
    /** A running test must not read as finished, or the floor looks greener than it is. */
    expect(completionOf(jobs("DONE", "RUNNING"))).toBe("partial");
    expect(completionOf(jobs("RUNNING", "NOT_RUN"))).toBe("partial");
    expect(completionOf(jobs("DONE", "NOT_RUN"))).toBe("partial");
  });

  it("distinguishes a node with no results at all from one that ran nothing", () => {
    expect(completionOf(undefined)).toBe("no_data");
    expect(completionOf(jobs())).toBe("no_data");
  });
});

describe("cellFill", () => {
  const views = heatmapViews(FLEET);
  const [statusView, completionView, metricView] = views;
  const range = metricRange(FLEET, "allreduce");

  it("uses the status palette on the status view", () => {
    expect(cellFill(statusView, FLEET.nodes[0], "down", range)).toBe(STATUS_FILL.down);
  });

  it("uses the completion palette on the completion view", () => {
    // r01-11: dcgmi DONE + allreduce DONE -> everything finished.
    expect(cellFill(completionView, FLEET.nodes[0], "up", range)).toBe(COMPLETION_FILL.all_done);
    // r02-11: dcgmi DONE + allreduce NOT_RUN -> started but unfinished.
    expect(cellFill(completionView, FLEET.nodes[2], "up", range)).toBe(COMPLETION_FILL.partial);
  });

  it("keeps a metric's magnitude by taking the shade the gauge would have ended on", () => {
    const fast = cellFill(metricView, FLEET.nodes[0], "up", range);
    const slow = cellFill(metricView, FLEET.nodes[1], "up", range);
    expect(fast).toBe(throughputColor(1));
    expect(fast).not.toBe(slow);
  });

  it("falls back to the empty track when nothing was measured", () => {
    expect(cellFill(metricView, FLEET.nodes[2], "up", range)).toBe(GAUGE_TRACK);
  });
});

describe("cellTone and toneCounts", () => {
  const [statusView, completionView, metricView] = heatmapViews(FLEET);

  it("reads tone from the question the current view is asking", () => {
    expect(cellTone(statusView, FLEET.nodes[0], "up")).toBe("good");
    expect(cellTone(statusView, FLEET.nodes[0], "down")).toBe("bad");
    expect(cellTone(completionView, FLEET.nodes[0], "up")).toBe("good");
    expect(cellTone(completionView, FLEET.nodes[2], "up")).toBe("warn");
    expect(cellTone(metricView, FLEET.nodes[1], "up")).toBe("bad");
    expect(cellTone(metricView, FLEET.nodes[2], "up")).toBe("none");
  });

  it("tallies tones so a rack summary cannot disagree with its own cells", () => {
    expect(toneCounts(["good", "good", "bad", "none"])).toEqual({
      good: 2, warn: 0, bad: 1, none: 1,
    });
  });
});

describe("rack row grouping", () => {
  it("reads the floor row off the rack id by dropping the rack number", () => {
    expect(rackRowOf("dh01-h01")).toBe("dh01-h");
    expect(rackRowOf("bos01-a12")).toBe("bos01-a");
  });

  it("returns no row when the id does not end in a number, rather than inventing one", () => {
    expect(rackRowOf("unassigned")).toBe("");
  });

  it("groups racks into rows in first-seen order", () => {
    expect(
      groupRacksByRow([{ rack: "dh01-h01" }, { rack: "dh01-h02" }, { rack: "dh01-i01" }]),
    ).toEqual([
      { row: "dh01-h", racks: [{ rack: "dh01-h01" }, { rack: "dh01-h02" }] },
      { row: "dh01-i", racks: [{ rack: "dh01-i01" }] },
    ]);
  });
});

describe("shortRackLabel", () => {
  it("drops the prefix the row heading already carries", () => {
    expect(shortRackLabel("ink50-dh01-h01")).toBe("h01");
    expect(shortRackLabel("bos01-a05")).toBe("a05");
  });

  it("keeps the id intact when there is nothing to drop", () => {
    expect(shortRackLabel("r01")).toBe("r01");
  });
});

describe("shortNodeLabel", () => {
  it("keeps the slot number an operator reads off the rack door", () => {
    expect(shortNodeLabel("dh01-h01-14", "dh01-h01")).toBe("14");
  });

  it("stays blank when the remainder would not fit in a cell", () => {
    /** Better an unlabelled square than a smear of unreadable text. */
    expect(shortNodeLabel("some-very-long-hostname", "rack")).toBe("");
  });
});

describe("chunkNodes", () => {
  it("splits a rack into fixed-width tray rows, short last row included", () => {
    expect(chunkNodes([1, 2, 3, 4, 5], 3)).toEqual([[1, 2, 3], [4, 5]]);
  });

  it("never divides by zero on a bad width", () => {
    expect(chunkNodes([1, 2], 0)).toEqual([[1], [2]]);
  });
});

describe("metric extraction", () => {
  it("reads the numeric metric and the verdict independently", () => {
    expect(metricOf(FLEET.nodes[0], "allreduce")).toBe(688.68);
    expect(verdictOf(FLEET.nodes[1], "allreduce")).toBe("FAIL");
    // A FAIL keeps its number upstream, so both must survive.
    expect(metricOf(FLEET.nodes[1], "allreduce")).toBe(2.28);
    expect(metricOf(FLEET.nodes[2], "allreduce")).toBeNull();
  });

  it("reports the observed range, ignoring nodes with no measurement", () => {
    expect(metricRange(FLEET, "allreduce")).toEqual({ min: 2.28, max: 688.68 });
    expect(metricRange(FLEET, "dcgmi-r1")).toBeNull();
  });
});

describe("paintCell", () => {
  const [statusView, , metricView] = heatmapViews(FLEET);
  const range = metricRange(FLEET, "allreduce");

  it("shows a full bar in the status colour on the status view", () => {
    expect(paintCell(statusView, FLEET.nodes[0], "up", null)).toMatchObject({
      bar: 1,
      barColor: STATUS_FILL.up,
    });
    expect(paintCell(statusView, FLEET.nodes[0], "down", null).barColor).toBe(STATUS_FILL.down);
  });

  it("sizes the bar from the value's position in the fleet range", () => {
    const top = paintCell(metricView, FLEET.nodes[0], "up", range);
    const low = paintCell(metricView, FLEET.nodes[1], "up", range);
    expect(top.bar).toBeCloseTo(1, 5);
    expect(low.bar).toBeLessThan(0.05);
    expect(top.barColor).not.toBe(low.barColor);
  });

  it("distinguishes an unmeasured node from one that measured zero", () => {
    /**
     * Both draw an empty bar, so the border is what separates them — a measured 0 passed its
     * gate, a NOT_RUN never ran. That is why the tint was removed: it said this twice, and said
     * it ambiguously for verdicts it did not recognise.
     */
    const notRun = paintCell(metricView, FLEET.nodes[2], "up", range);
    const zero = paintCell(metricView, node("x", "DONE", "0"), "up", range);
    expect(notRun.norm).toBeNull();
    expect(zero.norm).toBe(0);
    expect(notRun.bar).toBe(0);
    expect(zero.bar).toBe(0);
    expect(borderFor(metricView, FLEET.nodes[2], "up")).toBe(VERDICT_BORDER.NOT_RUN);
    expect(borderFor(metricView, node("x", "DONE", "0"), "up")).toBe(VERDICT_BORDER.PASS);
  });

  it("falls back to an empty cell for a node absent from the fleet feed", () => {
    const missing = paintCell(metricView, undefined, "unknown", range);
    expect(missing.norm).toBeNull();
    expect(missing.bar).toBe(0);
  });
});

describe("borderFor", () => {
  const [statusView, , metricView] = heatmapViews(FLEET);

  it("shows node up/down on the status view", () => {
    expect(borderFor(statusView, FLEET.nodes[0], "up")).toBe(STATUS_BORDER.up);
    expect(borderFor(statusView, FLEET.nodes[0], "down")).toBe(STATUS_BORDER.down);
  });

  it("shows the benchmark verdict on a metric view, green pass / red fail", () => {
    expect(borderFor(metricView, FLEET.nodes[0], "up")).toBe(VERDICT_BORDER.PASS);
    expect(borderFor(metricView, FLEET.nodes[1], "up")).toBe(VERDICT_BORDER.FAIL);
    expect(borderFor(metricView, FLEET.nodes[2], "up")).toBe(VERDICT_BORDER.NOT_RUN);
  });

  it("does not let a healthy node hide a failed benchmark", () => {
    /** The point of switching the border to the verdict: an UP node that failed reads as failed. */
    expect(borderFor(metricView, FLEET.nodes[1], "up")).not.toBe(STATUS_BORDER.up);
  });

  it("falls back to the unknown colour for an unrecognised or missing verdict", () => {
    expect(borderFor(metricView, node("odd", "WEIRD_STATE", null), "up")).toBe(
      VERDICT_BORDER_UNKNOWN,
    );
    expect(borderFor(metricView, undefined, "up")).toBe(VERDICT_BORDER_UNKNOWN);
  });
});

describe("GAUGE_GRADIENT", () => {
  it("runs left-to-right from near-white to deep blue with positioned stops", () => {
    /**
     * Anchored to the cell and masked to the fill length, so how much of it shows is the signal.
     * A solid per-value colour made near-ceiling results indistinguishable.
     */
    expect(GAUGE_GRADIENT).toMatch(/^linear-gradient\(to right,/);
    expect(GAUGE_GRADIENT).toContain("rgb(248,250,252) 0%");
    expect(GAUGE_GRADIENT).toContain("rgb(12,42,138) 100%");
    // Stops carry explicit percentages so the scale matches throughputColor().
    expect(GAUGE_GRADIENT).toContain("50%");
  });
});

describe("throughputColor", () => {
  it("is monotonic from near-white to a dark blue and clamps out-of-range input", () => {
    expect(throughputColor(0)).toBe("rgb(248,250,252)");
    expect(throughputColor(1)).toBe("rgb(12,42,138)");
    expect(throughputColor(-5)).toBe(throughputColor(0));
    expect(throughputColor(99)).toBe(throughputColor(1));
  });
});

describe("column ordering and sort ranks", () => {
  it("pins the headline benchmarks first and keeps the rest in place", () => {
    expect(
      orderColumns(["dcgmi-r1", "alltoall-nonvlink", "nvbandwidth", "alltoall", "allreduce"]),
    ).toEqual([
      "allreduce",
      "alltoall",
      "nvbandwidth",
      "dcgmi-r1",
      "alltoall-nonvlink",
    ]);
  });

  it("does not invent columns a site does not run", () => {
    expect(orderColumns(["dcgmi-r1", "allreduce"])).toEqual(["allreduce", "dcgmi-r1"]);
  });

  it("ranks problems before healthy results", () => {
    expect(verdictRank("FAIL")).toBeLessThan(verdictRank("RUNNING"));
    expect(verdictRank("RUNNING")).toBeLessThan(verdictRank("NOT_RUN"));
    expect(verdictRank("NOT_RUN")).toBeLessThan(verdictRank("DONE"));
    expect(statusRank("down")).toBeLessThan(statusRank("up"));
  });

  it("keeps unmeasured nodes last in both sort directions", () => {
    /** Otherwise "sort to find the slowest" returns a screen of blanks. */
    for (const dir of ["asc", "desc"] as const) {
      expect(compareMetric(null, 100, dir)).toBeGreaterThan(0);
      expect(compareMetric(100, null, dir)).toBeLessThan(0);
    }
    expect(compareMetric(1, 2, "asc")).toBeLessThan(0);
    expect(compareMetric(1, 2, "desc")).toBeGreaterThan(0);
    expect(compareMetric(null, null, "asc")).toBe(0);
  });
});

describe("verdictLabel", () => {
  it("uses the words operators use, and never calls an unfinished run a result", () => {
    expect(verdictLabel("DONE").label).toBe("Pass");
    expect(verdictLabel("FAIL").label).toBe("Failed");
    expect(verdictLabel("NOT_RUN").label).toBe("Untested");
    // RETRY/INCOMPLETE mean "no answer yet" from outside, so neither passes nor fails.
    expect(verdictLabel("RETRY").label).toBe("Running");
    expect(verdictLabel("INCOMPLETE").label).toBe("Running");
    expect(verdictLabel("").label).toBe("Untested");
  });
});

describe("groupByRack", () => {
  it("groups nodes by rack, preserving first-seen rack order", () => {
    const groups = groupByRack(
      ["r02-11", "r01-11", "r01-12"],
      (id) => id.split("-")[0],
    );
    expect(groups).toEqual([
      { rack: "r02", nodes: ["r02-11"] },
      { rack: "r01", nodes: ["r01-11", "r01-12"] },
    ]);
  });

  it("buckets nodes with no rack rather than dropping them", () => {
    expect(groupByRack(["lonely"], () => "")).toEqual([
      { rack: "unassigned", nodes: ["lonely"] },
    ]);
  });
});
