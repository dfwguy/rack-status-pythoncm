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
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
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

/** Wrapped in a router because node names link into Compute → Nodes. */
function renderTable(fleet: BringupFleet | null = FLEET) {
  return render(
    <MemoryRouter>
      <NodeMetricsTable fleet={fleet} nodes={NODES} rackOf={rackOf} />
    </MemoryRouter>,
  );
}

function cell(node: string, column: string) {
  return screen.getByTestId(`cell-${node}-${column}`);
}

/**
 * The header cell for a column. KUI Table makes headers selectable via onColumnSelect rather
 * than rendering a nested button, so the cell itself is what a test clicks.
 */
function headerFor(columnKey: string) {
  const marker = document.querySelector(`[data-column="${columnKey}"]`);
  const header = marker?.closest("th");
  if (!header) throw new Error(`no header for column ${columnKey}`);
  return header;
}

describe("NodeMetricsTable", () => {
  it("gives every node a row with its rack and up/down status", () => {
    renderTable();
    const table = screen.getByRole("table", { name: "Node benchmark results" });
    // 4 nodes + the header row.
    expect(within(table).getAllByRole("row")).toHaveLength(5);
    expect(within(table).getByText("r01-11")).toBeInTheDocument();
    expect(within(table).getAllByText("r01")).toHaveLength(4);
    // Scoped to the table: the status filter also renders "Up"/"Down" options.
    expect(within(table).getAllByText("Up")).toHaveLength(3);
    expect(within(table).getByText("Down")).toBeInTheDocument();
  });

  it("shows the measured number, not just a colour", () => {
    /** The reason this view exists: a gauge cannot separate 690 from 630 near the ceiling. */
    renderTable();
    expect(within(cell("r01-11", "allreduce")).getByText("690")).toBeInTheDocument();
    expect(within(cell("r01-12", "allreduce")).getByText("345")).toBeInTheDocument();
  });

  it("fills the gauge in proportion to the fleet max", () => {
    /**
     * The gradient spans the whole cell and this element masks the unfilled remainder, so the
     * fill length is where the mask starts — that is what makes a longer bar end in a darker
     * blue instead of every bar being one flat shade.
     */
    renderTable();
    // 690 is the observed max, 345 is exactly half of it.
    const full = screen.getByTestId("gauge-fill-r01-11-allreduce");
    const half = screen.getByTestId("gauge-fill-r01-12-allreduce");
    expect(full).toHaveAttribute("data-fraction", "1.000");
    expect(half).toHaveAttribute("data-fraction", "0.500");
    expect(full).toHaveStyle({ left: "100%" });
    expect(half).toHaveStyle({ left: "50%" });
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

  it("links each node into Compute → Nodes so a bad row is one click from its logs", () => {
    renderTable();
    const link = screen.getByRole("link", { name: "r01-11" });
    expect(link).toHaveAttribute("href", "/compute/nodes?node=r01-11");
  });

  it("encodes the node name in the link", () => {
    /** Node names are hostnames today, but the link must not break on one that needs escaping. */
    const odd: BringupFleet = { ...FLEET, nodes: [node("rack a/1", "DONE", "600")] };
    render(
      <MemoryRouter>
        <NodeMetricsTable fleet={odd} nodes={[]} rackOf={rackOf} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: "rack a/1" })).toHaveAttribute(
      "href",
      "/compute/nodes?node=rack%20a%2F1",
    );
  });

  it("pins the benchmarks operators read first, in order", () => {
    /** The bring-up CSV puts dcgm first and buries the throughput numbers. */
    const wide: BringupFleet = {
      ...FLEET,
      columns: ["dcgmi-r1", "alltoall-nonvlink", "nvbandwidth", "alltoall", "allreduce"],
      metric_labels: {
        "alltoall-nonvlink": "busbw GB/s",
        nvbandwidth: "min GB/s",
        alltoall: "busbw GB/s",
        allreduce: "busbw GB/s",
      },
      nodes: [node("r01-11", "DONE", "690")],
    };
    renderTable(wide);
    const keys = [...document.querySelectorAll("[data-column]")].map(
      (el) => el.getAttribute("data-column") ?? "",
    );
    const benchmarks = keys.filter((k) =>
      ["allreduce", "alltoall", "nvbandwidth", "alltoall-nonvlink"].includes(k),
    );
    expect(benchmarks).toEqual(["allreduce", "alltoall", "nvbandwidth", "alltoall-nonvlink"]);
  });

  describe("sorting", () => {
    function nodeColumnValues() {
      const table = screen.getByRole("table", { name: "Node benchmark results" });
      return within(table)
        .getAllByRole("row")
        .slice(1)
        .map((r) => r.querySelector("td")?.textContent ?? "");
    }

    it("sorts a benchmark by value, fastest first on the first click", () => {
      renderTable();
      fireEvent.click(headerFor("allreduce"));
      // 690 then 345; the two nodes with no value fall to the bottom.
      expect(nodeColumnValues().slice(0, 2)).toEqual(["r01-11", "r01-12"]);
    });

    it("reverses to slowest first, still keeping unmeasured nodes last", () => {
      /**
       * The point of pinning blanks to the bottom: "sort to find the slowest" must show real
       * numbers, not a screen of Untested rows.
       */
      renderTable();
      fireEvent.click(headerFor("allreduce"));
      fireEvent.click(headerFor("allreduce"));
      expect(nodeColumnValues().slice(0, 2)).toEqual(["r01-12", "r01-11"]);
    });

    it("sorts node status with the unhealthy first", () => {
      renderTable();
      fireEvent.click(headerFor("__status"));
      expect(nodeColumnValues()[0]).toBe("r01-13"); // the only DOWN node
    });
  });

  describe("filtering", () => {
    it("narrows by node or rack text", () => {
      renderTable();
      fireEvent.change(screen.getByLabelText("Filter by node or rack name"), {
        target: { value: "r01-12" },
      });
      const table = screen.getByRole("table", { name: "Node benchmark results" });
      expect(within(table).getAllByRole("row")).toHaveLength(2); // header + 1
      expect(screen.getByText("1 of 4 nodes")).toBeInTheDocument();
    });

    it("matches a rack fragment as well as a node name", () => {
      renderTable();
      fireEvent.change(screen.getByLabelText("Filter by node or rack name"), {
        target: { value: "r01" },
      });
      expect(screen.getByText("4 of 4 nodes")).toBeInTheDocument();
    });

    it("reports when a filter matches nothing rather than silently emptying", () => {
      renderTable();
      fireEvent.change(screen.getByLabelText("Filter by node or rack name"), {
        target: { value: "nosuchnode" },
      });
      expect(screen.getByText("0 of 4 nodes")).toBeInTheDocument();
    });
  });

  it("omits the pager when everything fits on one page", () => {
    /** KUI guidance: only render Pagination when totalItems > pageSize. */
    renderTable();
    expect(screen.queryByLabelText("Go to next page")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Page size")).not.toBeInTheDocument();
  });

  it("pages with the KUI pagination controls", () => {
    const many: BringupFleet = {
      ...FLEET,
      nodes: Array.from({ length: 120 }, (_, i) =>
        node(`r09-${String(i).padStart(3, "0")}`, "DONE", "600"),
      ),
    };
    render(
      <MemoryRouter>
        <NodeMetricsTable fleet={many} nodes={[]} rackOf={rackOf} />
      </MemoryRouter>,
    );
    const table = screen.getByRole("table", { name: "Node benchmark results" });
    expect(within(table).getByText("r09-000")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Go to next page"));
    expect(within(table).queryByText("r09-000")).not.toBeInTheDocument();
    expect(within(table).getByText("r09-050")).toBeInTheDocument();
    expect(screen.getByText(/51–100 of 120 nodes/)).toBeInTheDocument();
  });

  it("paginates rather than rendering an entire cluster at once", () => {
    const many: BringupFleet = {
      ...FLEET,
      nodes: Array.from({ length: 120 }, (_, i) => node(`r09-${i}`, "DONE", "600")),
    };
    render(
      <MemoryRouter>
        <NodeMetricsTable fleet={many} nodes={[]} rackOf={rackOf} />
      </MemoryRouter>,
    );
    const table = screen.getByRole("table", { name: "Node benchmark results" });
    // Default page size is 50, plus the header row.
    expect(within(table).getAllByRole("row")).toHaveLength(51);
    expect(screen.getByText(/1–50 of 120 nodes/)).toBeInTheDocument();
  });
});
