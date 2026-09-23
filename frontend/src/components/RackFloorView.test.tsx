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
import { describe, expect, it } from "vitest";
import { RackFloorView } from "./RackFloorView";
import type { RackRow, StatusScalableUnit } from "../types/api";

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
});
