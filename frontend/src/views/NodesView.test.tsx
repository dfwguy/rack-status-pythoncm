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
import type { RackStatusResponse } from "../types/api";

// Isolate NodesView table/filter/selection logic from the heavy action tabs,
// the parser/terminal dialogs, and the rack-command store.
// The provider lives in its own module now (the context object and hook stayed behind in
// RackCommandContext.ts), so the stub has to target that path or the real provider runs and
// throws for want of a RackCommandsStoreProvider.
vi.mock("../context/RackCommandProvider", () => ({
  RackCommandProvider: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
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
vi.mock("../components/rack/WebTerminalDialog", () => ({ default: () => null }));

// The page size comes from the active catalog geometry (one rack per page). The real provider
// is mounted at the app root; stub it so each test can state the geometry it is exercising.
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

describe("NodesView", () => {
  function visibleRowCount() {
    const table = screen.getByRole("table", { name: "Compute nodes" });
    // Subtract the header row.
    return within(table).getAllByRole("row").length - 1;
  }

  it("renders the page and falls back to 18 rows when no catalog geometry is active", () => {
    // 3 racks x 18 trays = 54 nodes; page size 18 keeps exactly one rack visible.
    renderNodes(status(["p5-r01", "p5-r02", "p5-r03"]));
    expect(screen.getByText("Nodes")).toBeInTheDocument();
    expect(screen.getByText("54 of 54 nodes")).toBeInTheDocument();
    expect(visibleRowCount()).toBe(18);
  });

  it("sizes the page from the active cluster geometry, not a hardcoded rack shape", () => {
    useGeometry(9);
    renderNodes(status(["p5-r01", "p5-r02", "p5-r03"]));
    expect(visibleRowCount()).toBe(9);
  });

  it("falls back to the platform geometry when the cluster does not override it", () => {
    useGeometry(4, "platform");
    renderNodes(status(["p5-r01", "p5-r02", "p5-r03"]));
    expect(visibleRowCount()).toBe(4);
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
