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
import { describe, it, expect } from "vitest";
import type { RackRow, RackSummary } from "./api";

describe("API types", () => {
  it("RackRow has expected fields", () => {
    const row: RackRow = {
      rack: "p5-r01",
      nemo: "done",
      last_job: "12345",
      sinfo: "idle=8",
      ct_down_unk: "UP:p5-r01-ct[01-08]",
    };
    expect(row.rack).toBe("p5-r01");
    expect(row.nemo).toBe("done");
  });

  it("RackSummary fields add up", () => {
    const summary: RackSummary = { done: 10, fail: 2, ongoing: 3, pending: 3 };
    const total = summary.done + summary.fail + summary.ongoing + summary.pending;
    expect(total).toBe(18);
  });
});
