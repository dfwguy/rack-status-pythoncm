import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { NodesView } from "./NodesView";
import type { RackStatusResponse } from "../types/api";

// Isolate NodesView table/filter/selection logic from the heavy action tabs,
// the parser/terminal dialogs, and the rack-command store.
vi.mock("../context/RackCommandContext", () => ({
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

function rack(id: string) {
  // 8 trays per rack via parseNodesFromRack default fill: ct01..ct02 UP, rest unknown.
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
  it("renders the page and defaults to 18 rows per page", () => {
    // 3 racks x 8 trays = 24 nodes; page size 18 => 18 visible, count says 24.
    render(<NodesView data={status(["p5-r01", "p5-r02", "p5-r03"])} logDir="/logs" />);
    expect(screen.getByText("Nodes")).toBeInTheDocument();
    expect(screen.getByText("24 of 24 nodes")).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Compute nodes" });
    // 18 body rows (data), plus the header row.
    const bodyRows = within(table).getAllByRole("row").length - 1;
    expect(bodyRows).toBe(18);
  });

  it("shows the select-a-node banner and no action tabs before selection", () => {
    render(<NodesView data={status(["p5-r01"])} logDir="/logs" />);
    expect(screen.getByText("Select a node above to run actions against it.")).toBeInTheDocument();
    expect(screen.queryByTestId("validation-tab")).not.toBeInTheDocument();
  });

  it("filters nodes by name", () => {
    render(<NodesView data={status(["p5-r01", "p5-r02"])} logDir="/logs" />);
    expect(screen.getByText("16 of 16 nodes")).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("Filter by node name…"), {
      target: { value: "r01-ct01" },
    });
    expect(screen.getByText("1 of 16 nodes")).toBeInTheDocument();
    expect(screen.getByText("p5-r01-ct01")).toBeInTheDocument();
  });

  it("enables the action tabs when a node is selected", () => {
    render(<NodesView data={status(["p5-r01"])} logDir="/logs" />);
    fireEvent.click(screen.getByText("p5-r01-ct01"));
    // Selected-node header + default (Validation) tab rendered, banner gone.
    expect(screen.queryByText("Select a node above to run actions against it.")).not.toBeInTheDocument();
    expect(screen.getByTestId("validation-tab")).toBeInTheDocument();
    // Switch to the Hardware tab.
    fireEvent.click(screen.getByRole("tab", { name: "Hardware" }));
    expect(screen.getByTestId("hardware-tab")).toBeInTheDocument();
  });
});
