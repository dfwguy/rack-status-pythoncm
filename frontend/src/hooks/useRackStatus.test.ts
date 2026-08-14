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
import { describe, it, expect, vi, beforeEach } from "vitest";

describe("useRackStatus", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("module exports useRackStatus function", async () => {
    const mod = await import("./useRackStatus");
    expect(typeof mod.useRackStatus).toBe("function");
  });
});
