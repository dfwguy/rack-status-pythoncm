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
/**
 * The floor view's two grouping axes.
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 *
 * A row is where racks physically are; an SU is which fabric they share. Nscale TX numbers
 * SU1-SU14 across two `dh` rows, so neither grouping nests inside the other and the view
 * offers both. The SU toggle appears only where the cluster actually models SUs.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RackFloorView } from "./RackFloorView";
import type { NodeInventoryEntry, RackRow, StatusScalableUnit } from "../types/api";

function rackRow(rack: string): RackRow {
  return { rack, nemo: "-", last_job: "-", sinfo: "idle=18", ct_down_unk: "-" };
}

const ROWS = [
  rackRow("ink50-dh01-h18"),
  rackRow("ink50-dh01-h19"),
  rackRow("ink50-dh02-h01"),
];

const UNITS: StatusScalableUnit[] = [
  { id: "su12", label: "SU12", racks: ["ink50-dh01-h18", "ink50-dh02-h01"] },
  { id: "su13", label: "SU13", racks: ["ink50-dh01-h19"] },
];

describe("RackFloorView grouping", () => {
  // The grouping choice is persisted, so each test must start from a clean slate.
  beforeEach(() => localStorage.clear());

  it("draws rows by default and offers no SU toggle without an SU model", () => {
    // Rows stay the default: it is what somebody walking the floor needs.
    render(<RackFloorView rows={ROWS} />);
    expect(screen.queryByRole("button", { name: /by su/i })).toBeNull();
    expect(screen.getAllByText(/^Row /).length).toBeGreaterThan(0);
  });

  it("offers the toggle when the cluster models SUs", () => {
    render(<RackFloorView rows={ROWS} scalableUnits={UNITS} />);
    expect(screen.getByRole("button", { name: /by row/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /by su/i })).toBeInTheDocument();
    // Still rows until asked otherwise.
    expect(screen.queryByText("SU12")).toBeNull();
  });

  it("switches to SU grouping and back", () => {
    render(<RackFloorView rows={ROWS} scalableUnits={UNITS} />);
    fireEvent.click(screen.getByRole("button", { name: /by su/i }));
    expect(screen.getByText("SU12")).toBeInTheDocument();
    expect(screen.getByText("SU13")).toBeInTheDocument();
    // Row headings are gone; the two axes are alternatives, not nested.
    expect(screen.queryByText(/^Row /)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /by row/i }));
    expect(screen.queryByText("SU12")).toBeNull();
  });

  it("shows an SU spanning two rows as one group", () => {
    // The case row grouping cannot express, and the reason SU is its own axis.
    render(<RackFloorView rows={ROWS} scalableUnits={UNITS} />);
    fireEvent.click(screen.getByRole("button", { name: /by su/i }));
    expect(screen.getByText("2 racks")).toBeInTheDocument();
    expect(screen.getByText("1 rack")).toBeInTheDocument();
  });

  it("surfaces racks the SU map does not mention", () => {
    // A rack on the floor before the map catches up. Hiding it would make the floor view
    // disagree with the rack count.
    render(
      <RackFloorView
        rows={ROWS}
        scalableUnits={[{ id: "su12", label: "SU12", racks: ["ink50-dh01-h18"] }]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /by su/i }));
    expect(screen.getByText("Unassigned")).toBeInTheDocument();
  });

  it("renders every rack exactly once in either grouping", () => {
    // The rack total is what operators read off this view.
    render(<RackFloorView rows={ROWS} scalableUnits={UNITS} />);
    // Counted by each tile's own "n/n up" line: the tile's heading is a short slot label,
    // so matching on the rack id would count nothing.
    const countTiles = () => screen.getAllByText(/\d+\/\d+ up/).length;
    const byRow = countTiles();
    expect(byRow).toBe(ROWS.length);
    fireEvent.click(screen.getByRole("button", { name: /by su/i }));
    expect(countTiles()).toBe(byRow);
  });

  it("restores the last grouping after the view is unmounted and remounted", () => {
    // Navigating to another page and back unmounts the floor view; By SU must survive.
    const first = render(<RackFloorView rows={ROWS} scalableUnits={UNITS} />);
    fireEvent.click(screen.getByRole("button", { name: /by su/i }));
    first.unmount();
    render(<RackFloorView rows={ROWS} scalableUnits={UNITS} />);
    expect(screen.getByText("SU12")).toBeInTheDocument();
  });
});

/**
 * The floor view's two status sources. BCM says whether the hardware is up; Slurm says whether it
 * can take work. On Nscale NC most racks are up to one and drained to the other, so the view lets
 * an operator flip between them over the same cells.
 */
describe("RackFloorView status source", () => {
  beforeEach(() => localStorage.clear());

  const RACK = "ink50-dh01-h18";
  // Listed out of tray order on purpose: cells follow the tray, not the payload order.
  const NODES: NodeInventoryEntry[] = [
    { id: "gpu3", rack: RACK, tray: 3, ip: "", status: "up", scheduler_state: "drain", scheduler_status: "drained" },
    { id: "gpu1", rack: RACK, tray: 1, ip: "", status: "up", scheduler_state: "alloc", scheduler_status: "running" },
    { id: "gpu2", rack: RACK, tray: 2, ip: "", status: "inst", scheduler_state: "idle", scheduler_status: "idle" },
    { id: "gpu4", rack: RACK, tray: 4, ip: "", status: "down", scheduler_state: "drain", scheduler_status: "drained" },
  ];
  const SOURCES = { orchestrator: "bcm", scheduler: "slurm" };
  const cellStates = () =>
    Array.from(document.querySelectorAll("[data-node-status]")).map((c) => c.getAttribute("data-node-status"));

  it("offers no status choice without a scheduler answer", () => {
    // An older server (no inventory) or a cluster no scheduler answered for: one view, no selector.
    render(<RackFloorView rows={ROWS} />);
    expect(screen.queryByRole("button", { name: /by scheduler/i })).toBeNull();
    render(<RackFloorView rows={ROWS} nodes={NODES} nodeSources={{ orchestrator: "bcm", scheduler: "" }} />);
    expect(screen.queryByRole("button", { name: /by scheduler/i })).toBeNull();
  });

  it("draws the orchestrator view by default, one cell per node in tray order", () => {
    render(<RackFloorView rows={ROWS} nodes={NODES} nodeSources={SOURCES} />);
    expect(screen.getByRole("button", { name: /by orchestrator/i })).toHaveAttribute("aria-pressed", "true");
    expect(cellStates()).toEqual(["up", "inst", "up", "down"]);
    // A node BCM is still installing has its own colour, and the legend says which.
    expect(screen.getByText("Installing")).toBeInTheDocument();
    expect(screen.getByTestId("floor-legend")).toHaveTextContent("BCM");
  });

  it("repaints the same cells with the scheduler's view", () => {
    render(<RackFloorView rows={ROWS} nodes={NODES} nodeSources={SOURCES} />);
    fireEvent.click(screen.getByRole("button", { name: /by scheduler/i }));
    expect(cellStates()).toEqual(["running", "idle", "drained", "drained"]);
    const legend = screen.getByTestId("floor-legend");
    expect(legend).toHaveTextContent("Slurm");
    for (const label of ["Idle", "Running", "Pending", "Reserved", "Draining", "Drained", "Down", "Unknown"]) {
      expect(legend).toHaveTextContent(label);
    }
    // The tile's count line asks the scheduler's question: how much of the rack can take work.
    expect(screen.getByText("1/4 idle")).toBeInTheDocument();
    // Racks the inventory does not cover keep their row counts rather than claiming "0 idle".
    expect(screen.getAllByText(/\d+\/\d+ up/)).toHaveLength(ROWS.length - 1);
  });

  it("names the raw scheduler state on each cell", () => {
    render(<RackFloorView rows={ROWS} nodes={NODES} nodeSources={SOURCES} />);
    fireEvent.click(screen.getByRole("button", { name: /by scheduler/i }));
    expect(screen.getByTitle("tray 3 gpu3: Drained (drain)")).toBeInTheDocument();
  });

  it("keeps the chosen view across a remount, but never one the cluster cannot show", () => {
    const first = render(<RackFloorView rows={ROWS} nodes={NODES} nodeSources={SOURCES} />);
    fireEvent.click(screen.getByRole("button", { name: /by scheduler/i }));
    first.unmount();
    const second = render(<RackFloorView rows={ROWS} nodes={NODES} nodeSources={SOURCES} />);
    expect(screen.getByRole("button", { name: /by scheduler/i })).toHaveAttribute("aria-pressed", "true");
    second.unmount();
    // The remembered choice is "scheduler", but this cluster has none: draw the orchestrator view.
    render(<RackFloorView rows={ROWS} nodes={NODES} nodeSources={{ orchestrator: "bcm", scheduler: "" }} />);
    expect(cellStates()).toEqual(["up", "inst", "up", "down"]);
  });

  it("puts the status choice below the grouping choice when both exist", () => {
    render(<RackFloorView rows={ROWS} scalableUnits={UNITS} nodes={NODES} nodeSources={SOURCES} />);
    const grouping = screen.getByRole("group", { name: /group racks/i });
    const status = screen.getByRole("group", { name: /node status source/i });
    expect(grouping.compareDocumentPosition(status) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // And the two are independent: SU grouping keeps the scheduler view.
    fireEvent.click(screen.getByRole("button", { name: /by scheduler/i }));
    fireEvent.click(screen.getByRole("button", { name: /by su/i }));
    expect(cellStates()).toEqual(["running", "idle", "drained", "drained"]);
  });
});

it("uses literal rack labels and no fabricated rows in the opt-in tile overview", () => {
  const rows = ["Q4", "Q25", "V19"].map((rack) => ({
    rack, nemo: "-", last_job: "-", ct_down_unk: "-", sinfo: "",
  }));
  const onSelect = vi.fn();
  render(<RackFloorView rows={rows} unconfiguredLayout="tiles" selectedRack="" onRackSelect={onSelect} />);
  expect(screen.getByRole("group", { name: "Rack tiles" })).toBeInTheDocument();
  expect(screen.queryByText(/^Row /)).toBeNull();
  for (const name of ["Q4", "Q25", "V19"]) {
    expect(screen.getByText(name)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Select rack " + name }));
  }
  expect(onSelect.mock.calls.map(([name]) => name)).toEqual(["Q4", "Q25", "V19"]);
  expect(screen.getAllByText("No mapped nodes")).toHaveLength(3);
});
