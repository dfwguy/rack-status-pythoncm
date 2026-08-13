import { describe, expect, it } from "vitest";
import { compareMetricsRacks, metricsRackMoniker } from "./metricsRack";

describe("metricsRackMoniker", () => {
  it("turns BCM warehouse rack labels into the short operator moniker", () => {
    expect(metricsRackMoniker("WH1D1")).toBe("d01");
    expect(metricsRackMoniker("WH1E1")).toBe("e01");
    expect(metricsRackMoniker("WH1F1")).toBe("f01");
    expect(metricsRackMoniker("WH1G1")).toBe("g01");
    expect(metricsRackMoniker("WH1G3")).toBe("g03");
    expect(metricsRackMoniker("WH1H1")).toBe("h01");
    expect(metricsRackMoniker("WH1J21")).toBe("j21");
  });

  it("canonicalises legacy short labels and preserves unknown labels", () => {
    expect(metricsRackMoniker("h2")).toBe("h02");
    expect(metricsRackMoniker("h03")).toBe("h03");
    expect(metricsRackMoniker("rack-west")).toBe("rack-west");
  });

  it("sorts by the displayed rack ordinal", () => {
    const racks = ["WH1G15", "WH1G2", "WH1G8", "WH1G1"];
    expect(racks.sort(compareMetricsRacks)).toEqual(["WH1G1", "WH1G2", "WH1G8", "WH1G15"]);
  });
});
