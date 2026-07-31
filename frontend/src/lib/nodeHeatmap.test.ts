import { describe, expect, it } from "vitest";
import {
  STATUS_BORDER,
  STATUS_FILL,
  STATUS_VIEW_ID,
  VERDICT_BORDER,
  VERDICT_BORDER_UNKNOWN,
  borderFor,
  compareMetric,
  groupByRack,
  heatmapViews,
  metricOf,
  metricRange,
  orderColumns,
  paintCell,
  statusRank,
  throughputColor,
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
  const [statusView, metricView] = heatmapViews(FLEET);

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
