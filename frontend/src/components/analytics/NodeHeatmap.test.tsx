import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { NodeHeatmap } from "./NodeHeatmap";
import { STATUS_BORDER, STATUS_FILL } from "../../lib/nodeHeatmap";
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

function renderHeatmap(props: Partial<React.ComponentProps<typeof NodeHeatmap>> = {}) {
  return render(
    <NodeHeatmap fleet={FLEET} nodes={NODES} rackOf={rackOf} {...props} />,
  );
}

function cell(id: string) {
  return screen.getByTestId(`heatmap-cell-${id}`);
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

  it("defaults to the up/down view", () => {
    renderHeatmap();
    expect(cell("r01-11")).toHaveStyle({ background: STATUS_FILL.up });
    expect(cell("r02-11")).toHaveStyle({ background: STATUS_FILL.down });
  });

  it("keeps up/down as the border so liveness survives a metric view", () => {
    /** The whole point of the border channel: a fast-but-down node must still read as down. */
    renderHeatmap();
    expect(cell("r02-11")).toHaveStyle({ border: `2px solid ${STATUS_BORDER.down}` });
    expect(cell("r01-11")).toHaveStyle({ border: `2px solid ${STATUS_BORDER.up}` });
  });

  it("describes each cell for assistive tech and hover", () => {
    renderHeatmap();
    expect(cell("r01-11")).toHaveAttribute("aria-label", "r01-11: status UP");
  });

  it("includes nodes the status feed does not know about", () => {
    /** Bring-up may know a node the dashboard has not reported; dropping it under-reports. */
    renderHeatmap({ nodes: [{ id: "r01-11", status: "up" }] });
    expect(cell("r02-11")).toBeInTheDocument();
    expect(cell("r02-11")).toHaveStyle({ background: STATUS_FILL.unknown });
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
});
