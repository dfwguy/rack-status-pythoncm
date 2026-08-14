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
import { COMPACT_RACKS_PER_ROW } from "./nodeHeatmap";
import {
  type HeatmapImageModel,
  imageFilename,
  layoutHeatmap,
  readableOn,
} from "./heatmapImage";

function rack(name: string, cellCount: number) {
  return {
    rack: name,
    cells: Array.from({ length: cellCount }, (_, i) => ({
      id: `${name}-${i + 11}`,
      fill: "#16a34a",
      border: "#15803d",
      label: String(i + 11),
    })),
  };
}

function model(racks: ReturnType<typeof rack>[]): HeatmapImageModel {
  return {
    title: "Node heatmap",
    subtitle: "cluster · now",
    legend: [{ label: "up", color: "#16a34a" }],
    racks,
  };
}

describe("layoutHeatmap", () => {
  it("tiles racks across the page and wraps onto a new band", () => {
    const racks = Array.from({ length: COMPACT_RACKS_PER_ROW + 2 }, (_, i) =>
      rack(`dh01-h${String(i + 1).padStart(2, "0")}`, 18),
    );
    const layout = layoutHeatmap(model(racks));

    const xs = layout.racks.map((r) => r.x);
    // The first row fills left to right, then the next rack returns to the left margin.
    expect(new Set(xs.slice(0, COMPACT_RACKS_PER_ROW)).size).toBe(COMPACT_RACKS_PER_ROW);
    expect(xs[COMPACT_RACKS_PER_ROW]).toBe(xs[0]);
    expect(layout.racks[COMPACT_RACKS_PER_ROW].y).toBeGreaterThan(layout.racks[0].y);
  });

  it("stacks an 18-node rack three across and six down", () => {
    const layout = layoutHeatmap(model([rack("dh01-h01", 18)]));
    const cells = layout.racks[0].cells;
    expect(cells).toHaveLength(18);
    expect(new Set(cells.map((c) => c.x)).size).toBe(3);
    expect(new Set(cells.map((c) => c.y)).size).toBe(6);
  });

  it("gives each floor row a heading and pushes later rows further down", () => {
    const layout = layoutHeatmap(model([rack("dh01-h01", 18), rack("dh01-i01", 18)]));
    expect(layout.rows.map((r) => r.row)).toEqual(["dh01-h", "dh01-i"]);
    expect(layout.rows[1].y).toBeGreaterThan(layout.rows[0].y);
  });

  it("sizes the canvas to hold everything it placed", () => {
    const layout = layoutHeatmap(model([rack("dh01-h01", 18), rack("dh01-i01", 18)]));
    const lastCell = layout.racks.at(-1)!.cells.at(-1)!;
    expect(layout.height).toBeGreaterThan(lastCell.y);
    expect(layout.width).toBeGreaterThan(layout.racks.at(-1)!.x);
  });

  it("handles a short rack without leaving a hole in the grid", () => {
    /** A partially populated rack is normal mid-bring-up and must not break the layout. */
    const layout = layoutHeatmap(model([rack("dh01-h01", 4)]));
    expect(layout.racks[0].cells).toHaveLength(4);
    expect(layout.height).toBeGreaterThan(0);
  });
});

describe("readableOn", () => {
  it("puts dark text on light fills and light text on dark ones", () => {
    /** Cells come from three different scales, so a fixed text colour fails on one of them. */
    expect(readableOn("#f8fafc")).toBe("#111827");
    expect(readableOn("#0c2a8a")).toBe("#ffffff");
    expect(readableOn("rgb(248,250,252)")).toBe("#111827");
    expect(readableOn("rgb(12,42,138)")).toBe("#ffffff");
  });

  it("stays legible when handed something it cannot parse", () => {
    expect(readableOn("transparent")).toBe("#ffffff");
  });
});

describe("imageFilename", () => {
  it("carries the cluster and date so downloads do not collide", () => {
    const name = imageFilename("Nscale TX", "png", new Date("2026-07-31T12:00:00Z"));
    expect(name).toBe("node-heatmap-nscale-tx-2026-07-31.png");
  });

  it("uses the conventional jpg extension for jpeg", () => {
    expect(imageFilename("x", "jpeg", new Date("2026-07-31T12:00:00Z"))).toMatch(/\.jpg$/);
  });

  it("still produces a usable name when the cluster is unnamed", () => {
    expect(imageFilename("", "png", new Date("2026-07-31T12:00:00Z"))).toBe(
      "node-heatmap-cluster-2026-07-31.png",
    );
  });
});
