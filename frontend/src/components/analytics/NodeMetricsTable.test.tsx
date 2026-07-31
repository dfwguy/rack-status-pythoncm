import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { NodeMetricsTable } from "./NodeMetricsTable";
import { FALLBACK_METRIC_MAX } from "../../lib/nodeHeatmap";
import type { BringupFleet } from "../../types/bringup";
import type { NodeInfo } from "../rack/NodeSelector";

function job(name: string, status: string, metric: string | null, unit: string | null) {
  return { name, base: name, status, reason: "", metric, metric_label: unit };
}

function node(name: string, allreduce: string, metric: string | null, dcgm = "DONE") {
  return {
    node: name,
    slurm_state: "idle",
    jobs: [job("dcgmi-r1", dcgm, null, null), job("allreduce", allreduce, metric, "busbw GB/s")],
  };
}

const FLEET: BringupFleet = {
  home: "/h",
  columns: ["dcgmi-r1", "allreduce"],
  metric_labels: { allreduce: "busbw GB/s" },
  nodes: [
    node("r01-11", "DONE", "690"),
    node("r01-12", "DONE", "345"),
    node("r01-13", "FAIL", null, "NOT_RUN"),
    node("r01-14", "RUNNING", null),
  ],
};

const NODES: NodeInfo[] = [
  { id: "r01-11", status: "up" },
  { id: "r01-12", status: "up" },
  { id: "r01-13", status: "down" },
  { id: "r01-14", status: "up" },
];

const rackOf = (id: string) => id.split("-")[0];

function renderTable(fleet: BringupFleet | null = FLEET) {
  return render(<NodeMetricsTable fleet={fleet} nodes={NODES} rackOf={rackOf} />);
}

function cell(node: string, column: string) {
  return screen.getByTestId(`cell-${node}-${column}`);
}

describe("NodeMetricsTable", () => {
  it("gives every node a row with its rack and up/down status", () => {
    renderTable();
    const table = screen.getByRole("table", { name: "Node benchmark results" });
    // 4 nodes + the header row.
    expect(within(table).getAllByRole("row")).toHaveLength(5);
    expect(screen.getByText("r01-11")).toBeInTheDocument();
    expect(screen.getAllByText("r01")).toHaveLength(4);
    expect(screen.getAllByText("Up")).toHaveLength(3);
    expect(screen.getByText("Down")).toBeInTheDocument();
  });

  it("shows the measured number, not just a colour", () => {
    /** The reason this view exists: a gauge cannot separate 690 from 630 near the ceiling. */
    renderTable();
    expect(within(cell("r01-11", "allreduce")).getByText("690")).toBeInTheDocument();
    expect(within(cell("r01-12", "allreduce")).getByText("345")).toBeInTheDocument();
  });

  it("fills the gauge in proportion to the fleet max", () => {
    renderTable();
    // 690 is the observed max, 345 is exactly half of it.
    expect(screen.getByTestId("gauge-fill-r01-11-allreduce")).toHaveStyle({ width: "100%" });
    expect(screen.getByTestId("gauge-fill-r01-12-allreduce")).toHaveStyle({ width: "50%" });
  });

  it("scales each benchmark against its own range", () => {
    /** A column header states the ceiling it used, so a colour is never ambiguous. */
    renderTable();
    expect(screen.getByText(/busbw GB\/s · 0–690/)).toBeInTheDocument();
  });

  it("falls back to a fixed ceiling when nothing has been measured", () => {
    const unmeasured: BringupFleet = {
      ...FLEET,
      nodes: [node("r01-11", "NOT_RUN", null, "NOT_RUN")],
    };
    renderTable(unmeasured);
    expect(screen.getByText(new RegExp(`0–${FALLBACK_METRIC_MAX}`))).toBeInTheDocument();
  });

  it("names the verdict in words rather than raw bring-up states", () => {
    renderTable();
    expect(within(cell("r01-13", "allreduce")).getByText("Failed")).toBeInTheDocument();
    expect(within(cell("r01-14", "allreduce")).getByText("Running")).toBeInTheDocument();
    // dcgmi has no metric, so it is a status-only column.
    expect(screen.getAllByText("Pass").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Untested").length).toBeGreaterThan(0);
  });

  it("shows a verdict instead of a zero gauge when there is no number", () => {
    /** A failure that produced nothing must not render as a measured 0. */
    renderTable();
    expect(screen.queryByTestId("gauge-fill-r01-13-allreduce")).not.toBeInTheDocument();
  });

  it("paginates rather than rendering an entire cluster at once", () => {
    const many: BringupFleet = {
      ...FLEET,
      nodes: Array.from({ length: 120 }, (_, i) => node(`r09-${i}`, "DONE", "600")),
    };
    render(<NodeMetricsTable fleet={many} nodes={[]} rackOf={rackOf} />);
    const table = screen.getByRole("table", { name: "Node benchmark results" });
    // Default page size is 50, plus the header row.
    expect(within(table).getAllByRole("row")).toHaveLength(51);
    expect(screen.getByText(/1–50 of 120/)).toBeInTheDocument();
  });
});
