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
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { NodesView } from "./NodesView";
import type { NodeInventoryEntry, RackStatusResponse } from "../types/api";

// Isolate NodesView table/filter/selection logic from the heavy action tabs,
// the parser/terminal dialogs, and the rack-command store.
// The provider lives in its own module now (the context object and hook stayed behind in
// RackCommandContext.ts), so the stub has to target that path or the real provider runs and
// throws for want of a RackCommandsStoreProvider.
vi.mock("../context/RackCommandProvider", () => ({
  RackCommandProvider: ({ children, rack, jobId }: { children: React.ReactNode; rack: string; jobId?: string }) => (
    <div data-testid="rack-command-provider" data-rack={rack} data-job={jobId}>{children}</div>
  ),
}));
vi.mock("../components/rack/ValidationTab", () => ({
  ValidationTab: () => <div data-testid="validation-tab" />,
}));
vi.mock("../components/rack/HardwareTab", () => ({
  HardwareTab: () => <div data-testid="hardware-tab" />,
}));
vi.mock("../components/rack/NetworkingTab", () => ({
  NetworkingTab: () => <div data-testid="networking-tab" />,
}));
vi.mock("../components/rack/ServicesTab", () => ({
  ServicesTab: () => <div data-testid="services-tab" />,
}));
vi.mock("../components/ParserResultsDialog", () => ({ ParserResultsDialog: () => null }));
vi.mock("../components/rack/WebTerminalDialog", () => ({
  default: ({ open, target }: { open: boolean; target: string }) => (
    open ? <div data-testid="node-terminal">{target}</div> : null
  ),
}));
vi.mock("../components/bringup/BringupResultsPanel", () => ({
  BringupResultsPanel: ({ node }: { node: string }) => <div data-testid="bringup-results" data-node={node} />,
}));

// Geometry applies only to the compatibility fallback for older servers.
const { catalogGeometry } = vi.hoisted(() => ({
  catalogGeometry: {
    current: { activeCluster: null, activePlatform: null } as {
      activeCluster: { geometry: { trays_per_rack: number } } | null;
      activePlatform: { geometry: { trays_per_rack?: number; gpus_per_node?: number } } | null;
    },
  },
}));
vi.mock("../context/ActiveClusterContext", () => ({
  useActiveCluster: () => catalogGeometry.current,
}));

function useGeometry(trays: number | null, from: "cluster" | "platform" = "cluster") {
  if (trays === null) {
    catalogGeometry.current = { activeCluster: null, activePlatform: null };
    return;
  }
  const entry = { geometry: { trays_per_rack: trays } };
  catalogGeometry.current =
    from === "cluster"
      ? { activeCluster: entry, activePlatform: null }
      : { activeCluster: null, activePlatform: entry };
}

beforeEach(() => useGeometry(null));

/**
 * NodesView reads ?node= to deep-link from the Analytics table, so it needs a router.
 * `route` sets the entry so a test can land on a specific node.
 */
function renderNodes(
  data: RackStatusResponse | null,
  { route = "/compute/nodes", logDir = "/logs" }: { route?: string; logDir?: string } = {},
) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <NodesView data={data} logDir={logDir} />
    </MemoryRouter>,
  );
}

function rack(id: string) {
  // ct01..ct02 are UP; the active catalog geometry fills the remaining trays as unknown.
  return { rack: id, nemo: "done", last_job: "1", sinfo: "idle=8", ct_down_unk: `UP:${id}-ct[01-02]` };
}

function status(rackIds: string[]): RackStatusResponse {
  const rows = rackIds.map(rack);
  return {
    ok: true,
    returncode: 0,
    updated_at: "2026-07-29T14:00:00Z",
    log_dir: "/logs",
    script: "collector",
    stderr: "",
    legend: "",
    rows,
    summary: { done: rows.length, fail: 0, ongoing: 0, pending: 0 },
    footnotes: [],
    comments: {},
    physical_rf0: {},
    native_workers: { mode: "test" },
    snapshot: {
      id: "snap-1",
      state: "fresh",
      cluster_id: "cluster-a",
      log_root: "/logs",
      configuration_generation: 1,
      last_attempt_at: "2026-07-29T14:00:00Z",
      last_success_at: "2026-07-29T13:59:00Z",
      age_seconds: 60,
      last_error: null,
    },
  };
}

function node(id: string, rackId: string | null = null): NodeInventoryEntry {
  return { id, rack: rackId, status: "up", ip: "" };
}

function chooseFilter(label: string, option: string) {
  const control = screen.getByRole("combobox", { name: label });
  const listboxId = control.getAttribute("aria-controls");
  const optionElement = screen.getAllByRole("option", { hidden: true }).find((element) => (
    element.closest(`#${listboxId}`) !== null && element.textContent === option
  ));
  if (!optionElement) throw new Error(`Missing ${label} option: ${option}`);
  fireEvent.click(optionElement);
}

describe("NodesView", () => {
  function visibleRowCount() {
    const table = screen.getByRole("table", { name: "Compute nodes" });
    // Subtract the header row.
    return within(table).getAllByRole("row").length - 1;
  }

  it("retains rack expansion for older servers and uses a fixed page size", () => {
    // Older servers omit nodes; only that compatibility path expands rack geometry.
    renderNodes(status(["p5-r01", "p5-r02", "p5-r03"]));
    expect(screen.getByText("Nodes")).toBeInTheDocument();
    expect(screen.getByText("54 of 54 nodes")).toBeInTheDocument();
    expect(visibleRowCount()).toBe(25);
  });

  it("uses cluster geometry only for legacy node expansion", () => {
    useGeometry(9);
    renderNodes(status(["p5-r01", "p5-r02", "p5-r03"]));
    expect(screen.getByText("27 of 27 nodes")).toBeInTheDocument();
    expect(visibleRowCount()).toBe(25);
  });

  it("uses platform geometry for legacy expansion when the cluster does not override it", () => {
    useGeometry(4, "platform");
    renderNodes(status(["p5-r01", "p5-r02", "p5-r03"]));
    expect(screen.getByText("12 of 12 nodes")).toBeInTheDocument();
    expect(visibleRowCount()).toBe(12);
  });

  it("shows discovered nodes when no racks exist, with node-scoped actions", () => {
    renderNodes({ ...status([]), nodes: [{ ...node("rtx-worker-01"), ip: "10.1.2.3" }] });
    expect(screen.getByText("1 of 1 nodes")).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Compute nodes" });
    expect(within(table).getByText("Unassigned")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "10.1.2.3" })).toHaveAttribute("href", "https://10.1.2.3");

    fireEvent.click(within(table).getByText("rtx-worker-01"));
    expect(screen.getByTestId("bringup-results")).toHaveAttribute("data-node", "rtx-worker-01");
    expect(screen.getByTestId("rack-command-provider")).toHaveAttribute("data-rack", "rtx-worker-01");
    expect(screen.getByTestId("validation-tab")).toBeInTheDocument();
    expect(screen.getByText(/Actions apply to this node only/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Open terminal to rtx-worker-01" }));
    expect(screen.getByTestId("node-terminal")).toHaveTextContent("rtx-worker-01");
  });

  it("treats an empty authoritative inventory as empty despite configured racks", () => {
    renderNodes({ ...status(["p5-r01"]), nodes: [] });
    expect(screen.getByText("0 of 0 nodes")).toBeInTheDocument();
    expect(screen.getByText("No nodes reported yet.")).toBeInTheDocument();
    expect(screen.queryByText("p5-r01-ct01")).not.toBeInTheDocument();
  });

  it("shows unavailable inventory without falling back to fabricated rack slots", () => {
    renderNodes({ ...status(["p5-r01"]), nodes: null, node_inventory_error: "Node discovery failed: controller unavailable." });
    expect(screen.getByText("Node inventory is unavailable.")).toBeInTheDocument();
    expect(screen.getByText("Node discovery failed: controller unavailable.")).toBeInTheDocument();
    expect(screen.queryByText("p5-r01-ct01")).not.toBeInTheDocument();
  });

  it("retains reported nodes while displaying a discovery error", () => {
    renderNodes({ ...status([]), nodes: [node("rtx-worker-01")], node_inventory_error: "Showing previous node inventory: controller unavailable." });
    expect(screen.getByText("rtx-worker-01")).toBeInTheDocument();
    expect(screen.getByText("Showing previous node inventory: controller unavailable.")).toBeInTheDocument();
  });

  it("lists only discovered nodes in a partially populated rack and preserves its action context", () => {
    useGeometry(18);
    renderNodes({ ...status(["p5-r01"]), nodes: [node("p5-r01-ct07", "p5-r01")] });
    expect(screen.getByText("1 of 1 nodes")).toBeInTheDocument();
    expect(screen.queryByText("p5-r01-ct01")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("p5-r01-ct07"));
    expect(screen.getByTestId("rack-command-provider")).toHaveAttribute("data-rack", "p5-r01");
    expect(screen.getByTestId("rack-command-provider")).toHaveAttribute("data-job", "1");
    expect(screen.getByTestId("validation-tab")).toBeInTheDocument();
  });

  it.each(["custom-worker-7", "p5-r01-custom-07"])("uses exact-host actions when %s does not fit the rack template", (hostname) => {
    renderNodes({ ...status(["p5-r01"]), nodes: [node(hostname, "p5-r01")] });
    fireEvent.click(screen.getByText(hostname));
    expect(screen.getByTestId("rack-command-provider")).toHaveAttribute("data-rack", "p5-r01");
    expect(screen.getByTestId("validation-tab")).toBeInTheDocument();
    expect(screen.getAllByRole("tab")).toHaveLength(4);
    expect(screen.getByTestId("bringup-results")).toHaveAttribute("data-node", hostname);
    fireEvent.click(screen.getByRole("button", { name: `Open terminal to ${hostname}` }));
    expect(screen.getByTestId("node-terminal")).toHaveTextContent(hostname);
  });

  it("filters assigned and unassigned nodes without conflating a rack named unassigned", () => {
    renderNodes({
      ...status(["p5-r01", "unassigned"]),
      nodes: [node("worker-01", "p5-r01"), node("worker-02"), node("worker-03", "unassigned")],
    });
    const table = screen.getByRole("table", { name: "Compute nodes" });
    expect(screen.getByText("3 of 3 nodes")).toBeInTheDocument();
    chooseFilter("Filter by rack", "Unassigned");
    expect(screen.getByText("1 of 3 nodes")).toBeInTheDocument();
    expect(within(table).getByText("worker-02")).toBeInTheDocument();
    expect(within(table).queryByText("worker-03")).not.toBeInTheDocument();
    chooseFilter("Filter by rack", "unassigned");
    expect(screen.getByText("2 of 3 nodes")).toBeInTheDocument();
    expect(within(table).getByText("worker-03")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("Filter by node name…"), { target: { value: "worker-02" } });
    expect(screen.getByText("1 of 3 nodes")).toBeInTheDocument();
  });

  it("paginates discovered hosts independently of rack geometry", () => {
    useGeometry(2);
    renderNodes({ ...status([]), nodes: Array.from({ length: 30 }, (_, index) => node(`worker-${index + 1}`)) });
    expect(visibleRowCount()).toBe(25);
    fireEvent.click(screen.getByRole("button", { name: "Go to next page" }));
    expect(visibleRowCount()).toBe(5);
    expect(screen.getByText("worker-26")).toBeInTheDocument();
  });

  it("uses rack hierarchy for assigned node row filters", () => {
    renderNodes({
      ...status(["rack-01"]),
      nodes: [node("worker-01", "rack-01"), node("worker-02")],
      hierarchy: {
        levels: ["row", "rack"],
        sections: [{
          key: "", label: "", rows: [{ key: "row-a", label: "A", racks: [{ rack: "rack-01", label: "01" }] }],
        }],
        unmatched: [],
      },
    });
    chooseFilter("Filter by datacenter row", "A");
    expect(screen.getByText("1 of 2 nodes")).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Compute nodes" });
    expect(within(table).getByText("worker-01")).toBeInTheDocument();
    expect(within(table).queryByText("worker-02")).not.toBeInTheDocument();
  });

  describe("deep link from the Analytics node table", () => {
    it("opens straight to the linked node, already selected", () => {
      renderNodes(status(["p5-r01", "p5-r02"]), {
        route: "/compute/nodes?node=p5-r02-ct01",
      });
      // Selected: the actions are live rather than showing the pick-a-node banner.
      expect(
        screen.queryByText("Select a node above to run actions against it."),
      ).not.toBeInTheDocument();
      expect(screen.getByTestId("validation-tab")).toBeInTheDocument();
    });

    it("filters to the linked node so its row is not buried on a later page", () => {
      renderNodes(status(["p5-r01", "p5-r02"]), {
        route: "/compute/nodes?node=p5-r02-ct01",
      });
      expect(screen.getByText("1 of 36 nodes")).toBeInTheDocument();
    });

    it("behaves normally when no node is linked", () => {
      renderNodes(status(["p5-r01"]));
      expect(
        screen.getByText("Select a node above to run actions against it."),
      ).toBeInTheDocument();
    });
  });

  it("shows the select-a-node banner and no action tabs before selection", () => {
    renderNodes(status(["p5-r01"]));
    expect(screen.getByText("Select a node above to run actions against it.")).toBeInTheDocument();
    expect(screen.queryByTestId("validation-tab")).not.toBeInTheDocument();
  });

  it("filters nodes by name", () => {
    renderNodes(status(["p5-r01", "p5-r02"]));
    expect(screen.getByText("36 of 36 nodes")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("Filter by node name…"), {
      target: { value: "r01-ct01" },
    });
    expect(screen.getByText("1 of 36 nodes")).toBeInTheDocument();
    expect(screen.getByText("p5-r01-ct01")).toBeInTheDocument();
  });

  it("enables the action tabs when a node is selected", () => {
    renderNodes(status(["p5-r01"]));
    fireEvent.click(screen.getByText("p5-r01-ct01"));
    // Selected-node header + default (Validation) tab rendered, banner gone.
    expect(screen.queryByText("Select a node above to run actions against it.")).not.toBeInTheDocument();
    expect(screen.getByTestId("validation-tab")).toBeInTheDocument();
    // Switch to the Hardware tab.
    fireEvent.click(screen.getByRole("tab", { name: "Hardware" }));
    expect(screen.getByTestId("hardware-tab")).toBeInTheDocument();
  });

  describe("GPU count against the platform design", () => {
    function withGpus(expected: number | null) {
      catalogGeometry.current = {
        activeCluster: null,
        activePlatform: expected === null ? null : { geometry: { gpus_per_node: expected } },
      };
    }
    const nodesWith = (counts: Array<number | undefined>) =>
      counts.map((gpu_count, i) => ({ ...node(`gpu-${i}`), gpu_count }));

    it("shows reported/expected, green when they match and red when they differ", () => {
      withGpus(4);
      renderNodes({ ...status([]), nodes: nodesWith([4, 3]) });
      const table = screen.getByRole("table", { name: "Compute nodes" });
      const ok = within(table).getByText("4/4");
      const bad = within(table).getByText("3/4");
      expect(ok).toHaveClass("nv-tag--color-green");
      expect(bad).toHaveClass("nv-tag--color-red");
      expect(ok).toHaveAttribute("aria-label", expect.stringContaining("platform design is 4"));
      expect(bad).toHaveAttribute("aria-label", expect.stringContaining("3 GPUs reported"));
    });

    it("greys a node whose scheduler reported no GPU count instead of calling it a mismatch", () => {
      withGpus(4);
      renderNodes({ ...status([]), nodes: nodesWith([undefined]) });
      const tag = within(screen.getByRole("table", { name: "Compute nodes" })).getByText("—/4");
      expect(tag).toHaveAttribute("aria-label", expect.stringContaining("No GPU count reported"));
      expect(tag).toHaveClass("nv-tag--color-gray");
    });

    it("falls back to the plain number when there is no platform to compare with", () => {
      withGpus(null);
      renderNodes({ ...status([]), nodes: nodesWith([8]) });
      const table = screen.getByRole("table", { name: "Compute nodes" });
      expect(within(table).getByText("8")).toBeInTheDocument();
      expect(within(table).queryByText("8/0")).not.toBeInTheDocument();
    });
  });

  describe("column sorting", () => {
    // Node ids chosen so name order differs from every other column's order.
    const fleet = (): NodeInventoryEntry[] => [
      { ...node("n-a", "rack-10"), status: "up", gpu_count: 4 },
      { ...node("n-b", "rack-2"), status: "down", gpu_count: 3 },
      { ...node("n-c"), status: "inst" },
      { ...node("n-d", "rack-2"), status: "unknown", gpu_count: 4 },
    ];
    const hierarchy = {
      levels: ["row", "rack"],
      sections: [{
        key: "", label: "", rows: [
          { key: "row-b", label: "B", racks: [{ rack: "rack-10", label: "10" }] },
          { key: "row-a", label: "A", racks: [{ rack: "rack-2", label: "2" }] },
        ],
      }],
      unmatched: [],
    };
    const view = () => renderNodes({ ...status(["rack-2", "rack-10"]), nodes: fleet(), hierarchy });
    /** Node ids in the order the table shows them. */
    const order = () =>
      within(screen.getByRole("table", { name: "Compute nodes" }))
        .getAllByText(/^n-[a-d]$/).map((el) => el.textContent);
    const header = (name: string) => screen.getByRole("button", { name: new RegExp(`^${name}$`) });

    it("starts sorted by node name, and a second click reverses it", () => {
      view();
      expect(order()).toEqual(["n-a", "n-b", "n-c", "n-d"]);
      fireEvent.click(header("Node"));
      expect(order()).toEqual(["n-d", "n-c", "n-b", "n-a"]);
    });

    it("sorts by rack naturally, with unassigned last in both directions", () => {
      view();
      fireEvent.click(header("Rack"));
      expect(order()).toEqual(["n-b", "n-d", "n-a", "n-c"]);
      fireEvent.click(header("Rack"));
      expect(order()).toEqual(["n-a", "n-b", "n-d", "n-c"]);
    });

    it("sorts by datacenter row, blank rows last", () => {
      view();
      fireEvent.click(header("Row"));
      expect(order()).toEqual(["n-b", "n-d", "n-a", "n-c"]);
      fireEvent.click(header("Row"));
      expect(order()).toEqual(["n-a", "n-b", "n-d", "n-c"]);
    });

    it("sorts by GPU count, nodes with no count last in both directions", () => {
      view();
      fireEvent.click(header("GPUs"));
      expect(order()).toEqual(["n-b", "n-a", "n-d", "n-c"]);
      fireEvent.click(header("GPUs"));
      expect(order()).toEqual(["n-a", "n-d", "n-b", "n-c"]);
    });

    it("sorts by status with problems first, then reverses", () => {
      view();
      fireEvent.click(header("Status"));
      expect(order()).toEqual(["n-b", "n-c", "n-d", "n-a"]);
      fireEvent.click(header("Status"));
      expect(order()).toEqual(["n-a", "n-d", "n-c", "n-b"]);
    });

    it("marks only the active column as sorted (aria-sort)", () => {
      view();
      fireEvent.click(header("GPUs"));
      const cells = within(screen.getByRole("table", { name: "Compute nodes" })).getAllByRole("columnheader");
      expect(cells.map((c) => c.getAttribute("aria-sort"))).toEqual([null, null, null, "ascending", null, null, null]);
    });
  });
});
