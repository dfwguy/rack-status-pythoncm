import { describe, expect, it } from "vitest";
import {
  STATUS_FILL,
  STATUS_VIEW_ID,
  groupByRack,
  heatmapViews,
  metricOf,
  metricRange,
  paintCell,
  throughputColor,
  verdictOf,
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
  it("offers node status first, then one view per measurable column", () => {
    const views = heatmapViews(FLEET);
    expect(views.map((v) => v.id)).toEqual([STATUS_VIEW_ID, "allreduce"]);
    // dcgmi-r1 produces no number, so it is not a heatmap view.
    expect(views[1].unit).toBe("busbw GB/s");
  });

  it("still offers the status view when there is no fleet data", () => {
    expect(heatmapViews(null).map((v) => v.id)).toEqual([STATUS_VIEW_ID]);
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
  const [statusView, metricView] = heatmapViews(FLEET);
  const range = metricRange(FLEET, "allreduce");

  it("colours the status view by up/down", () => {
    expect(paintCell(statusView, FLEET.nodes[0], "up", null).fill).toBe(STATUS_FILL.up);
    expect(paintCell(statusView, FLEET.nodes[0], "down", null).fill).toBe(STATUS_FILL.down);
  });

  it("scales a measured value against the fleet range", () => {
    const top = paintCell(metricView, FLEET.nodes[0], "up", range);
    const low = paintCell(metricView, FLEET.nodes[1], "up", range);
    expect(top.norm).toBeCloseTo(1, 5);
    expect(low.norm).toBeLessThan(0.05);
    expect(top.fill).not.toBe(low.fill);
  });

  it("does not paint an unmeasured node as zero", () => {
    /** "never ran" and "measured 0" are different facts; colouring them alike invents a result. */
    const notRun = paintCell(metricView, FLEET.nodes[2], "up", range);
    const zero = paintCell(metricView, node("x", "DONE", "0"), "up", range);
    expect(notRun.norm).toBeNull();
    expect(zero.norm).toBe(0);
    expect(notRun.fill).not.toBe(zero.fill);
  });

  it("falls back to a not-run colour for a node absent from the fleet feed", () => {
    expect(paintCell(metricView, undefined, "unknown", range).norm).toBeNull();
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
