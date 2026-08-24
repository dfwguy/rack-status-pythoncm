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
import { schedulerJobIdForAction, slurmJobIdForScancel } from "./slurmHelpers";

describe("schedulerJobIdForAction", () => {
  it("accepts PythonCM ProgramRunner UUIDs", () => {
    expect(schedulerJobIdForAction("8ac9ea41-f683-4ac8-b00e-4368f9330a64")).toBe(
      "8ac9ea41-f683-4ac8-b00e-4368f9330a64",
    );
  });

  it("retains legacy Slurm numeric and array ids", () => {
    expect(schedulerJobIdForAction("147892")).toBe("147892");
    expect(slurmJobIdForScancel("147892_3")).toBe("147892_3");
  });

  it("rejects shell text and malformed ids", () => {
    expect(schedulerJobIdForAction("147892; reboot")).toBe("");
    expect(schedulerJobIdForAction("not-a-task")).toBe("");
  });
});
