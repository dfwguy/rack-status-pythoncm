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
import { schedulerNodeReasonsTable } from "./parseSinfoRStdout";

describe("schedulerNodeReasonsTable", () => {
  it("preserves a multiword PythonCM reason without fixed-width text parsing", () => {
    const parsed = schedulerNodeReasonsTable([
      {
        node: "ink50-dh01-d01-11",
        state: "drain",
        reason: "Ethernet rail degraded after validation",
        actor: "acceptance-simulator",
        timestamp: "2026-08-25T01:02:03Z",
      },
    ]);

    expect(parsed.useRaw).toBe(false);
    expect(parsed.headers).toEqual(["STATE", "REASON", "USER", "TIMESTAMP", "NODELIST"]);
    expect(parsed.rows[0]).toEqual([
      "drain",
      "Ethernet rail degraded after validation",
      "acceptance-simulator",
      "2026-08-25T01:02:03Z",
      "ink50-dh01-d01-11",
    ]);
  });
});
