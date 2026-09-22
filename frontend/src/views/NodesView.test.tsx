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
      activePlatform: { geometry: { trays_per_rack: number } } | null;
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

  it("shows discovered nodes when no racks exist, with terminal and results access", () => {
    renderNodes({ ...status([]), nodes: [{ ...node("rtx-worker-01"), ip: "10.1.2.3" }] });
    expect(screen.getByText("1 of 1 nodes")).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Compute nodes" });
    expect(within(table).getByText("Unassigned")).toBeInTheDocument();
    expect(within(table).getByRole("link", { name: "10.1.2.3" })).toHaveAttribute("href", "https://10.1.2.3");

    fireEvent.click(within(table).getByText("rtx-worker-01"));
    expect(screen.getByTestId("bringup-results")).toHaveAttribute("data-node", "rtx-worker-01");
    expect(screen.getByText(/This node has no rack assignment/)).toBeInTheDocument();
    expect(screen.queryByTestId("rack-command-provider")).not.toBeInTheDocument();
    expect(screen.queryByTestId("validation-tab")).not.toBeInTheDocument();

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

  it.each(["custom-worker-7", "p5-r01-custom-07"])("does not expose rack mutations when %s cannot be targeted individually", (hostname) => {
    renderNodes({ ...status(["p5-r01"]), nodes: [node(hostname, "p5-r01")] });
    fireEvent.click(screen.getByText(hostname));
    expect(screen.getByText("The available actions cannot target this hostname individually. Terminal access and node results remain available.")).toBeInTheDocument();
    expect(screen.queryByTestId("rack-command-provider")).not.toBeInTheDocument();
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
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
});
