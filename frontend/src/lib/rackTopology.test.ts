import { describe, expect, it } from "vitest";
import { buildFloorLayout, floorLayoutFromHierarchy } from "./rackTopology";
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
