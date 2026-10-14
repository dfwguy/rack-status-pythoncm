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
import { REPORT } from "../__fixtures__/report";
import { NO_FILTER, countShown, formatPercent, formatUtc, segments } from "./trackerModel";

describe("formatPercent", () => {
  it("rounds down so one node left never reads 100%", () => {
    expect(formatPercent(99.9)).toBe("99%");
    expect(formatPercent(100)).toBe("100%");
  });
  it("keeps a decimal for early progress and floors nonsense at zero", () => {
    expect(formatPercent(0.4)).toBe("0.4%");
    expect(formatPercent(0)).toBe("0%");
    expect(formatPercent(Number.NaN)).toBe("0%");
  });
});

describe("segments", () => {
  it("lists only non-zero states, in display order, as shares of the total", () => {
    const out = segments(REPORT.units[0].counts);
    expect(out.map((s) => s.state)).toEqual(["completed", "in_progress", "remediation", "not_started"]);
    expect(out[0].share).toBe(25);
  });
  it("is empty for an empty group", () => {
    expect(segments({ completed: 0, in_progress: 0, remediation: 0, not_started: 0, handed_over: 0 })).toEqual([]);
  });
});

describe("countShown", () => {
  it("counts every node with no filter", () => {
    expect(countShown(REPORT.units, NO_FILTER)).toBe(5);
  });
  it("narrows by state, unit and search, together", () => {
    expect(countShown(REPORT.units, { ...NO_FILTER, state: "remediation" })).toBe(1);
    expect(countShown(REPORT.units, { ...NO_FILTER, unit: "su19" })).toBe(1);
    // Search reaches the reason as well as names: "fabric" finds the node that failed on it.
    expect(countShown(REPORT.units, { ...NO_FILTER, query: "fabric" })).toBe(1);
    // Every term must match: the SU and the rack together narrow to that rack.
    expect(countShown(REPORT.units, { ...NO_FILTER, query: "su20 v20" })).toBe(2);
    expect(countShown(REPORT.units, { ...NO_FILTER, unit: "su19", state: "remediation" })).toBe(0);
  });
});

describe("formatUtc", () => {
  it("prints minute precision in UTC whatever the reader's zone", () => {
    expect(formatUtc("2026-10-13T12:00:00+00:00")).toBe("2026-10-13 12:00 UTC");
    expect(formatUtc("")).toBe("—");
  });
});
