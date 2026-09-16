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
import { buildMetricsRackOptions, compareMetricsRacks, metricsRackMoniker } from "./metricsRack";

describe("metricsRackMoniker", () => {
  it("combines the actual SU label with warehouse and expansion-hall rack coordinates", () => {
    expect(metricsRackMoniker("EH1A1", "SU1")).toBe("SU1A01");
    expect(metricsRackMoniker("EH1B17", "su2")).toBe("SU2B17");
    expect(metricsRackMoniker("WH1D1", "SU12")).toBe("SU12D01");
    expect(metricsRackMoniker("WH1J21", "SU0012")).toBe("SU12J21");
  });

  it("canonicalises legacy short labels and has a safe no-SU fallback", () => {
    expect(metricsRackMoniker("h2", "SU12")).toBe("SU12H02");
    expect(metricsRackMoniker("WH1H2")).toBe("H02");
    expect(metricsRackMoniker("h03")).toBe("H03");
    expect(metricsRackMoniker("rack-west")).toBe("rack-west");
  });

  it("sorts by the displayed rack ordinal", () => {
    const racks = ["WH1G15", "WH1G2", "WH1G8", "WH1G1"];
    expect(racks.sort(compareMetricsRacks)).toEqual(["WH1G1", "WH1G2", "WH1G8", "WH1G15"]);
  });

  it("builds unique menus sorted naturally by SU and rack", () => {
    expect(
      buildMetricsRackOptions([
        { rack: "WH1A1", su: "SU12" },
        { rack: "EH1B1", su: "SU2" },
        { rack: "EH1A10", su: "SU1" },
        { rack: "EH1A2", su: "SU1" },
        { rack: "EH1A2" },
        {},
      ]),
    ).toEqual([
      { rack: "EH1A2", su: "SU1", label: "SU1A02" },
      { rack: "EH1A10", su: "SU1", label: "SU1A10" },
      { rack: "EH1B1", su: "SU2", label: "SU2B01" },
      { rack: "WH1A1", su: "SU12", label: "SU12A01" },
    ]);
  });
});
