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
import { describe, expect, it } from "vitest";
import { barColumns, buildFloorLayout, buildScalableUnitLayout, floorLayoutFromHierarchy, rackHealth } from "./rackTopology";
import type { RackRow, RackHierarchy } from "../types/api";

function rackRow(rack: string): RackRow {
  return { rack, nemo: "-", last_job: "-", sinfo: "idle=18", ct_down_unk: "-" };
}

describe("floorLayoutFromHierarchy (backend-authoritative)", () => {
  it("groups Boston racks into rows a and b from the backend tree", () => {
    const rows = ["a", "b"].flatMap((r) =>
      [1, 5, 11].map((i) => rackRow(`bos01-${r}${String(i).padStart(2, "0")}`)),
    );
    const hierarchy: RackHierarchy = {
      levels: ["row", "rack"],
      unmatched: [],
      sections: [
        {
          key: "",
          label: "Cluster",
          rows: [
            {
              key: "bos01-a",
              label: "a",
              racks: [
                { rack: "bos01-a01", label: "01" },
                { rack: "bos01-a05", label: "05" },
                { rack: "bos01-a11", label: "11" },
              ],
            },
            {
              key: "bos01-b",
              label: "b",
              racks: [
                { rack: "bos01-b01", label: "01" },
                { rack: "bos01-b05", label: "05" },
                { rack: "bos01-b11", label: "11" },
              ],
            },
          ],
        },
      ],
    };

    const sections = floorLayoutFromHierarchy(rows, hierarchy);
    expect(sections).toHaveLength(1);
    expect(sections[0].rows.map((r) => r.label)).toEqual(["a", "b"]);
    expect(sections[0].rackCount).toBe(6);
    // rack tiles keep their backend label and join to the live status row
    expect(sections[0].rows[0].racks.map((r) => r.parts.rackLabel)).toEqual(["01", "05", "11"]);
    expect(sections[0].rows[0].racks[0].row.sinfo).toBe("idle=18");
  });
});

describe("buildFloorLayout fallback (alpha-aware client parser)", () => {
  it("still groups row-letter racks when only the template is available", () => {
    const rows = ["a", "a", "b"].map((r, i) =>
      rackRow(`bos01-${r}${String(i + 1).padStart(2, "0")}`),
    );
    const sections = buildFloorLayout(rows, {
      rack_id: "bos01-[A-Z][01-99]",
      ct_id: "bos01-[A-Z][01-99]-[01-99]-s1-dgx-[01-99]-c[01-99]",
      nvsw_id: "",
      ct_joiner: "-ct",
      node_id_mode: "rack_ct_suffix",
    });
    // one cluster section with two rows (a, b), not 3 single-rack rows
    const labels = sections.flatMap((s) => s.rows.map((r) => r.label)).sort();
    expect(labels).toEqual(["a", "b"]);
  });
});

describe("buildScalableUnitLayout (SU as its own grouping axis)", () => {
  // An SU genuinely does not follow the floor: Nscale TX numbers SU1-SU14 across two `dh`
  // rows, so SU is a separate axis from section -> row rather than a level inside it.
  const nscale = (row: string, slot: number) =>
    rackRow(`ink50-${row}-h${String(slot).padStart(2, "0")}`);

  it("groups racks by the map the backend sent, in the site's own order", () => {
    const rows = [nscale("dh01", 18), nscale("dh01", 19), nscale("dh02", 1)];
    const units = buildScalableUnitLayout(rows, [
      { id: "su12", label: "SU12", racks: ["ink50-dh01-h19", "ink50-dh01-h18"] },
      { id: "su01", label: "SU1", racks: ["ink50-dh02-h01"] },
    ]);
    expect(units.map((u) => u.label)).toEqual(["SU12", "SU1"]);
    // Two rows' worth of racks in one SU, which is the case row grouping cannot express.
    expect(units[0].rackCount).toBe(2);
  });

  it("spans rows within a single SU", () => {
    const rows = [nscale("dh01", 18), nscale("dh02", 1)];
    const units = buildScalableUnitLayout(rows, [
      { id: "su01", label: "SU1", racks: ["ink50-dh01-h18", "ink50-dh02-h01"] },
    ]);
    expect(units).toHaveLength(1);
    expect(units[0].racks.map((r) => r.row.rack)).toEqual([
      "ink50-dh01-h18",
      "ink50-dh02-h01",
    ]);
  });

  it("collects racks the map does not mention into a trailing Unassigned group", () => {
    // A real bring-up state: a rack reaches the floor before the SU map catches up.
    // Hiding it would make the floor view quietly disagree with the rack count.
    const rows = [nscale("dh01", 18), nscale("dh01", 19)];
    const units = buildScalableUnitLayout(rows, [
      { id: "su12", label: "SU12", racks: ["ink50-dh01-h18"] },
    ]);
    expect(units.map((u) => u.id)).toEqual(["su12", "__unassigned"]);
    expect(units[1].racks.map((r) => r.row.rack)).toEqual(["ink50-dh01-h19"]);
  });

  it("skips an SU whose racks are all absent from this snapshot", () => {
    // The backend reports such an SU; the view decides not to draw an empty tile row.
    const units = buildScalableUnitLayout(
      [nscale("dh01", 18)],
      [
        { id: "su12", label: "SU12", racks: ["ink50-dh01-h18"] },
        { id: "su09", label: "SU9", racks: ["ink50-dh09-h01"] },
      ],
    );
    expect(units.map((u) => u.id)).toEqual(["su12"]);
  });

  it("never draws a rack twice when the map claims it in two SUs", () => {
    // The Cluster model refuses that record, but the view must not double-count if a
    // hand-edited document reaches it anyway -- the rack total is what operators read.
    const rows = [nscale("dh01", 18)];
    const units = buildScalableUnitLayout(rows, [
      { id: "su12", label: "SU12", racks: ["ink50-dh01-h18"] },
      { id: "su13", label: "SU13", racks: ["ink50-dh01-h18"] },
    ]);
    expect(units.reduce((sum, u) => sum + u.rackCount, 0)).toBe(1);
  });

  it("matches rack ids case-insensitively", () => {
    const units = buildScalableUnitLayout(
      [rackRow("INK50-DH01-H18")],
      [{ id: "su12", label: "SU12", racks: ["ink50-dh01-h18"] }],
    );
    expect(units[0].rackCount).toBe(1);
  });

  it("returns nothing when the cluster has no SU map", () => {
    expect(buildScalableUnitLayout([nscale("dh01", 18)], [])).toEqual([]);
  });
});

describe("rackHealth — provider (BCM/pythoncm) node states", () => {
  const row = (sinfo: string): RackRow =>
    ({ rack: "U19", nemo: "-", last_job: "-", sinfo, ct_down_unk: "-" });

  it("counts installing nodes toward the total but not as up", () => {
    // An all-installing rack must read 0/18, not 0/0 (no data) and not 18/18 (misleading up).
    const h = rackHealth(row("inst=18"));
    expect(h.total).toBe(18);
    expect(h.upCount).toBe(0);
    expect(h.health).toBe("down");
  });

  it("mixes installing, down and up into a partial verdict", () => {
    const h = rackHealth(row("inst=16,down=1,up=1"));
    expect(h.total).toBe(18);
    expect(h.upCount).toBe(1);
    expect(h.health).toBe("partial");
  });

  it("still reads an all-up rack as healthy (Slurm path unchanged)", () => {
    const h = rackHealth(row("idle=18"));
    expect(h.total).toBe(18);
    expect(h.upCount).toBe(18);
    expect(h.health).toBe("healthy");
  });
});

describe("barColumns — node-count bar wraps into pleasant rows", () => {
  it("uses rows of 8 for multiples of 8", () => {
    expect(barColumns(8)).toBe(8);   // 1x8
    expect(barColumns(16)).toBe(8);  // 2x8 — the familiar look
    expect(barColumns(24)).toBe(8);  // 3x8
    expect(barColumns(72)).toBe(8);  // 9x8
  });

  it("wraps an off-multiple count into ~2 rows capped at 9 wide", () => {
    expect(barColumns(18)).toBe(9);  // NC's GB300 rack -> 2x9, not a thin 18-strip
    expect(barColumns(14)).toBe(7);  // 2x7
  });

  it("never returns less than 1 column", () => {
    expect(barColumns(1)).toBe(1);
    expect(barColumns(0)).toBe(1);
  });
});
