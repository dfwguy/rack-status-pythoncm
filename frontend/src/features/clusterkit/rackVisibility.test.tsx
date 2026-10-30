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
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RackSelector } from "./RackSelector";
import { RunComposer } from "./RunComposer";
import * as service from "./service";
import { completed, inventory, nodes, preview, readiness } from "./testing/fixtures";
import type { RackDirectory } from "./types";

vi.mock("./service", () => ({
  confirmRun: vi.fn(), previewRun: vi.fn(), getRackDirectory: vi.fn(), getInventory: vi.fn(),
}));

const directory: RackDirectory = {
  racks: [
    { name: "R1", node_count: 8, nodes, counts: { idle: 8, busy: 0, drain: 0, down: 0, reserved: 0, unknown: 0 },
      allowed_node_count: 8, allowed_node_counts: [1, 2, 4, 8], authoritative: true, test_enabled: true, reason: "" },
    { name: "R2", node_count: 2, nodes: ["unapproved-1", "unapproved-2"], counts: { idle: 2, busy: 0, drain: 0, down: 0, reserved: 0, unknown: 0 },
      allowed_node_count: 0, allowed_node_counts: [], authoritative: true, test_enabled: false, reason: "Testing is not enabled by the site profile" },
    { name: "R3", node_count: 0, nodes: [], counts: { idle: 0, busy: 0, drain: 0, down: 0, reserved: 0, unknown: 0 },
      allowed_node_count: 0, allowed_node_counts: [], authoritative: false, test_enabled: false, reason: "No authoritative node membership" },
  ],
  nodes: nodes.map((id, index) => ({ id, rack: "R1", status: "up", ip: "", tray: index + 1,
    scheduler_status: "idle", scheduler_state: "idle" })),
  snapshot: { state: "fresh", source: "Rack Status canonical inventory", age_seconds: 4 },
  node_sources: { orchestrator: "bcm", scheduler: "slurm" }, unplaced_node_count: 1,
};
const rackReadiness = { ...readiness, rack_inventory_enabled: true, rack_inventory_ready: true };

beforeEach(() => {
  vi.mocked(service.getRackDirectory).mockResolvedValue(structuredClone(directory));
  vi.mocked(service.getInventory).mockImplementation(async (_signal, group) => ({
    ...structuredClone(inventory), group,
    nodes: group === "R1" ? structuredClone(inventory.nodes)
      : [{ node: "unapproved-1", states: ["IDLE"], partitions: ["validation"], allowed: false }],
  }));
  vi.mocked(service.previewRun).mockResolvedValue(structuredClone(preview));
  vi.mocked(service.confirmRun).mockResolvedValue(completed);
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.restoreAllMocks(); });

describe("comprehensive rack visibility", () => {
  it("shows enabled and non-enabled racks, unmapped racks and source freshness", () => {
    render(<RackSelector directory={directory} loading={false} error="" selected="" onSelect={vi.fn()} onRefresh={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Select rack R1" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Select rack R2" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Select rack R3" })).toBeEnabled();
    expect(screen.getByText(/4s old/)).toBeInTheDocument();
    expect(screen.getByText(/1 inventory nodes have no physical rack placement/)).toBeInTheDocument();
    expect(screen.getByText("Not enabled")).toBeInTheDocument();
    expect(screen.getByText("No placement")).toBeInTheDocument();
  });
  it("filters by rack name and idle availability", () => {
    render(<RackSelector directory={directory} loading={false} error="" selected="" onSelect={vi.fn()} onRefresh={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Search racks"), { target: { value: "r2" } });
    expect(screen.queryByRole("button", { name: "Select rack R1" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Select rack R2" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search racks"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "With idle nodes" }));
    expect(screen.queryByRole("button", { name: "Select rack R3" })).not.toBeInTheDocument();
  });
  it("reuses the scheduler floor view and propagates selection", () => {
    const onSelect = vi.fn();
    render(<RackSelector directory={directory} loading={false} error="" selected="" onSelect={onSelect} onRefresh={vi.fn()} />);
    fireEvent.click(within(screen.getByLabelText("Rack inventory layout")).getByText("Tiles"));
    expect(screen.getByRole("group", { name: "Node status source" })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Rack tiles" })).toBeInTheDocument();
    expect(screen.queryByText(/^Row /)).not.toBeInTheDocument();
    expect(screen.getByText("R1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Select rack R1" }));
    expect(onSelect).toHaveBeenCalledWith("R1");
  });
  it("shows stale inventory without implying it can launch tests", () => {
    render(<RackSelector directory={{ ...directory, snapshot: { ...directory.snapshot, state: "stale", age_seconds: 1000 } }}
      loading={false} error="" selected="" onSelect={vi.fn()} onRefresh={vi.fn()} />);
    expect(screen.getByText(/Inventory is stale or unavailable/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Select rack R1" })).toBeInTheDocument();
  });
});

async function selectedComposer() {
  render(<RunComposer readiness={rackReadiness} inventory={null} onSubmit={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Select rack R1" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Preview run" })).toBeEnabled());
}
describe("rack-first ClusterKit composition", () => {
  it("requires a rack before preview and refreshes only the selected rack", async () => {
    render(<RunComposer readiness={rackReadiness} inventory={null} onSubmit={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Preview run" })).toBeDisabled();
    expect(service.getInventory).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "Select rack R1" }));
    await waitFor(() => expect(service.getInventory).toHaveBeenCalledWith(expect.any(AbortSignal), "R1"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Preview run" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Preview run" }));
    await screen.findByRole("button", { name: "Confirm & run" });
    expect(service.previewRun).toHaveBeenCalledWith({ scope: "group", group: "R1", node_count: 2, path_comparison: true }, expect.any(AbortSignal));
    expect(service.confirmRun).not.toHaveBeenCalled();
  });
  it("preflights one currently usable node from the selected rack", async () => {
    await selectedComposer();
    fireEvent.click(within(screen.getByLabelText("Node count")).getByText("1", { exact: true }));
    fireEvent.click(screen.getByRole("button", { name: "Preview run" }));
    await screen.findByRole("button", { name: "Confirm & run" });
    expect(service.previewRun).toHaveBeenCalledWith({ scope: "preflight", group: "R1", node_count: 1 }, expect.any(AbortSignal));
  });
  it("keeps a non-approved rack inspectable and prevents launch", async () => {
    await selectedComposer();
    fireEvent.click(screen.getByRole("button", { name: "Select rack R2" }));
    await screen.findByText("Testing is not enabled by the site profile");
    expect(screen.getByRole("button", { name: "Preview run" })).toBeDisabled();
    expect(service.previewRun).not.toHaveBeenCalled();
  });
  it("invalidates a confirmation when changing racks", async () => {
    await selectedComposer();
    fireEvent.click(screen.getByRole("button", { name: "Preview run" }));
    await screen.findByRole("button", { name: "Confirm & run" });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Select rack R2" })); });
    expect(screen.queryByRole("button", { name: "Confirm & run" })).not.toBeInTheDocument();
  });
  it("discards a late preview from the previous rack", async () => {
    await selectedComposer();
    let resolve!: (value: typeof preview) => void;
    vi.mocked(service.previewRun).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    fireEvent.click(screen.getByRole("button", { name: "Preview run" }));
    fireEvent.click(screen.getByRole("button", { name: "Select rack R2" }));
    await act(async () => { resolve(preview); });
    expect(screen.queryByRole("button", { name: "Confirm & run" })).not.toBeInTheDocument();
    expect(service.confirmRun).not.toHaveBeenCalled();
  });
  it("reports inventory errors and never uses the previous rack's nodes", async () => {
    await selectedComposer();
    vi.mocked(service.getInventory).mockRejectedValueOnce(new Error("Selected rack discovery timed out"));
    fireEvent.click(screen.getByRole("button", { name: "Select rack R2" }));
    await screen.findByText("Selected rack discovery timed out");
    expect(screen.getByRole("button", { name: "Preview run" })).toBeDisabled();
  });
  it("invalidates an existing plan when inventory is refreshed", async () => {
    await selectedComposer();
    fireEvent.click(screen.getByRole("button", { name: "Preview run" }));
    await screen.findByRole("button", { name: "Confirm & run" });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Refresh rack inventory" })); });
    expect(screen.queryByRole("button", { name: "Confirm & run" })).not.toBeInTheDocument();
  });
});
