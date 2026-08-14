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
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { NodeHeatmap } from "./NodeHeatmap";
import { STATUS_BORDER, STATUS_FILL } from "../../lib/nodeHeatmap";

/** jsdom has no 2D canvas context, so the drawing itself is covered in heatmapImage.test.ts. */
const { downloadSpy } = vi.hoisted(() => ({ downloadSpy: vi.fn() }));
vi.mock("../../lib/heatmapImage", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../lib/heatmapImage")>()),
  downloadHeatmapImage: downloadSpy,
}));
import type { BringupFleet } from "../../types/bringup";
import type { NodeInfo } from "../rack/NodeSelector";

function fleetNode(name: string, status: string, metric: string | null) {
  return {
    node: name,
    slurm_state: "idle",
    jobs: [
      {
        name: "allreduce", base: "allreduce", status,
        reason: status === "FAIL" ? "below floor" : "",
        metric, metric_label: "busbw GB/s",
      },
    ],
  };
}

const FLEET: BringupFleet = {
  home: "/home/cmods/bringup",
  columns: ["allreduce"],
  metric_labels: { allreduce: "busbw GB/s" },
  nodes: [
    fleetNode("r01-11", "DONE", "688.68"),
    fleetNode("r01-12", "FAIL", "2.28"),
    fleetNode("r02-11", "NOT_RUN", null),
  ],
};

const NODES: NodeInfo[] = [
  { id: "r01-11", status: "up" },
  { id: "r01-12", status: "up" },
  { id: "r02-11", status: "down" },
];

const rackOf = (id: string) => id.split("-")[0];

/** Wrapped in a router: the table mode links node names into Compute → Nodes. */
function renderHeatmap(props: Partial<React.ComponentProps<typeof NodeHeatmap>> = {}) {
  return render(
    <MemoryRouter>
      <NodeHeatmap fleet={FLEET} nodes={NODES} rackOf={rackOf} {...props} />
    </MemoryRouter>,
  );
}

function cell(id: string) {
  return screen.getByTestId(`heatmap-cell-${id}`);
}

/** The load bar inside a cell: its width is the metric, its colour the intensity. */
function bar(id: string) {
  return screen.getByTestId(`heatmap-bar-${id}`);
}

/** The Compact switch: on tiles racks as blocks, off gives each rack a row with gauges. */
function compactSwitch() {
  return screen.getByLabelText("Compact");
}

/**
 * Render and switch to the linear layout.
 *
 * The compact layout is the default because it fits the floor on one screen, but it has no room
 * for a gauge — anything asserting on bar geometry has to ask for the wide cells.
 */
function renderLinear(props: Partial<React.ComponentProps<typeof NodeHeatmap>> = {}) {
  const result = renderHeatmap(props);
  fireEvent.click(compactSwitch());
  return result;
}

describe("NodeHeatmap", () => {
  it("renders one cell per node, grouped by rack", () => {
    renderHeatmap();
    expect(cell("r01-11")).toBeInTheDocument();
    expect(cell("r01-12")).toBeInTheDocument();
    expect(cell("r02-11")).toBeInTheDocument();
    expect(screen.getByText("r01")).toBeInTheDocument();
    expect(screen.getByText("r02")).toBeInTheDocument();
    expect(screen.getByText(/3 nodes across 2 racks/)).toBeInTheDocument();
  });

  it("defaults to the up/down view, drawn as a full bar in the status colour", () => {
    renderLinear();
    expect(bar("r01-11")).toHaveStyle({ background: STATUS_FILL.up, width: "100%" });
    expect(bar("r02-11")).toHaveStyle({ background: STATUS_FILL.down });
  });

  it("keeps up/down as the border so liveness survives a metric view", () => {
    /** The whole point of the border channel: a fast-but-down node must still read as down. */
    renderLinear();
    expect(cell("r02-11")).toHaveStyle({ border: `2px solid ${STATUS_BORDER.down}` });
    expect(cell("r01-11")).toHaveStyle({ border: `2px solid ${STATUS_BORDER.up}` });
  });

  it("describes each cell for assistive tech and hover", () => {
    renderHeatmap();
    expect(cell("r01-11")).toHaveAttribute("aria-label", "r01-11: status UP");
  });

  it("includes nodes the status feed does not know about", () => {
    /** Bring-up may know a node the dashboard has not reported; dropping it under-reports. */
    renderLinear({ nodes: [{ id: "r01-11", status: "up" }] });
    expect(cell("r02-11")).toBeInTheDocument();
    expect(bar("r02-11")).toHaveStyle({ background: STATUS_FILL.unknown });
  });

  it("offers every metric for a node, not just the selected view", () => {
    /**
     * When a cell looks wrong, the next question is what else is true about that node.
     *
     * KUI's Tooltip is built on the native Popover API and renders its content into the DOM up
     * front, so the content is assertable without simulating hover.
     */
    renderHeatmap();
    expect(screen.getByText("r01-12")).toBeInTheDocument();
    expect(screen.getByText("2.28 busbw GB/s")).toBeInTheDocument();
    // The failure reason travels with it.
    expect(screen.getByText(/below floor/)).toBeInTheDocument();
  });

  it("says so when a node has no bring-up results", () => {
    renderHeatmap({ nodes: [{ id: "ghost-01", status: "up" }], fleet: { ...FLEET, nodes: [] } });
    expect(screen.getByText("No bring-up results for this node.")).toBeInTheDocument();
  });

  it("surfaces an error without hiding the panel", () => {
    renderHeatmap({ error: "bring-up fleet report timed out" });
    expect(screen.getByText("bring-up fleet report timed out")).toBeInTheDocument();
  });

  it("shows a loading state before any data arrives", () => {
    renderHeatmap({ fleet: null, nodes: [], loading: true });
    expect(screen.getByText(/Collecting bring-up results/)).toBeInTheDocument();
  });

  it("says so when there is nothing to draw", () => {
    renderHeatmap({ fleet: null, nodes: [] });
    expect(screen.getByText("No nodes to display yet.")).toBeInTheDocument();
  });

  it("refreshes on request", () => {
    const onRefresh = vi.fn();
    renderHeatmap({ onRefresh });
    fireEvent.click(screen.getByLabelText("Refresh heatmap"));
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("switches between the heatmap and the table", async () => {
    renderHeatmap();
    expect(cell("r01-11")).toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Node benchmark results" })).toBeNull();

    fireEvent.click(screen.getByRole("radio", { name: "Table" }));
    expect(
      await screen.findByRole("table", { name: "Node benchmark results" }),
    ).toBeInTheDocument();
    // The grid gives way to the table rather than both rendering.
    expect(screen.queryByTestId("heatmap-cell-r01-11")).toBeNull();
    // A single-metric selector is meaningless when every column is shown.
    expect(screen.queryByRole("combobox", { name: "Heatmap data view" })).toBeNull();
  });

  it("exposes a data-view selector reflecting the active view", () => {
    /** Which views exist is covered in nodeHeatmap.test.ts; here it just has to be wired up. */
    renderHeatmap();
    const selector = screen.getByRole("combobox", { name: "Heatmap data view" });
    expect(selector).toHaveTextContent("Node status (up / down)");
  });

  it("explains the status colours while the status view is active", () => {
    renderHeatmap();
    for (const label of ["up", "down", "installing", "unknown"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it("defaults to tiling racks as blocks so a whole floor fits on one screen", () => {
    renderHeatmap();
    expect(screen.getByTestId("heatmap-rack-r01")).toBeInTheDocument();
    expect(screen.getByTestId("heatmap-rack-r02")).toBeInTheDocument();
    // Compact cells are solid squares; the gauge belongs to the linear layout.
    expect(screen.queryByTestId("heatmap-bar-r01-11")).toBeNull();
  });

  it("abbreviates the rack label but keeps the full id on hover", () => {
    /** 49 blocks repeating the cluster prefix is noise, and it squeezes the tile. */
    renderHeatmap({
      nodes: [{ id: "ink50-dh01-h01-11", status: "up" }],
      fleet: { ...FLEET, nodes: [] },
      rackOf: (id) => id.slice(0, id.lastIndexOf("-")),
    });
    expect(screen.getByTestId("heatmap-rack-ink50-dh01-h01")).toBeInTheDocument();
    // The heading shows only the tail; the full id is the hover title.
    const heading = screen.getByTitle("ink50-dh01-h01");
    expect(heading.textContent).toBe("h01");
    // The row heading is what carries the prefix instead.
    expect(screen.getByText("ink50-dh01-h")).toBeInTheDocument();
  });

  it("labels each compact cell with its slot number", () => {
    /** Two digits is what fits, and it is what an operator reads off the rack door. */
    renderHeatmap();
    expect(cell("r01-11")).toHaveTextContent("11");
    expect(cell("r01-12")).toHaveTextContent("12");
  });

  it("summarises each rack so a mixed block says how mixed", () => {
    /** Colour alone says "mixed"; the tally says whether that is one node or half the rack. */
    renderHeatmap();
    const block = screen.getByTestId("heatmap-rack-r01");
    // r01 holds two up nodes on the default status view.
    expect(block).toHaveTextContent("2");
  });

  it("switches to the linear layout and back", () => {
    renderHeatmap();
    expect(compactSwitch()).toBeChecked();
    expect(screen.queryByTestId("heatmap-bar-r01-11")).toBeNull();

    fireEvent.click(compactSwitch());
    expect(compactSwitch()).not.toBeChecked();
    expect(bar("r01-11")).toBeInTheDocument();

    fireEvent.click(compactSwitch());
    expect(compactSwitch()).toBeChecked();
    expect(screen.queryByTestId("heatmap-bar-r01-11")).toBeNull();
  });

  it("offers the test-completion view alongside status and the benchmarks", () => {
    /** Dennis's report question -- how far through the plan is each node -- as a first-class view. */
    renderHeatmap();
    expect(screen.getByRole("combobox", { name: "Heatmap data view" })).toBeInTheDocument();
    expect(screen.getByText(/Test completion/)).toBeInTheDocument();
  });

  it("keeps every node addressable for assistive tech in the compact layout", () => {
    renderHeatmap();
    expect(cell("r02-11")).toHaveAttribute("aria-label", "r02-11: status DOWN");
  });

  describe("image download", () => {
    beforeEach(() => downloadSpy.mockReset());

    it("exports every rack and cell currently on the map", async () => {
      renderHeatmap({ clusterName: "Nscale TX" });
      fireEvent.click(screen.getByRole("button", { name: /Download image/ }));

      await waitFor(() => expect(downloadSpy).toHaveBeenCalledTimes(1));
      const [model, format, cluster] = downloadSpy.mock.calls[0];
      expect(format).toBe("png");
      expect(cluster).toBe("Nscale TX");
      expect(model.racks.map((r: { rack: string }) => r.rack)).toEqual(["r01", "r02"]);
      expect(model.racks[0].cells.map((c: { id: string }) => c.id)).toEqual(["r01-11", "r01-12"]);
      // The caption has to carry what the picture cannot: which cluster, and when.
      expect(model.subtitle).toContain("Nscale TX");
      expect(model.title).toContain("Node status");
      expect(model.legend.length).toBeGreaterThan(0);
    });

    it("reports a failure to build the image instead of failing silently", async () => {
      downloadSpy.mockRejectedValueOnce(new Error("canvas unavailable"));
      renderHeatmap();
      fireEvent.click(screen.getByRole("button", { name: /Download image/ }));

      expect(await screen.findByText(/canvas unavailable/)).toBeInTheDocument();
    });
  });
});
