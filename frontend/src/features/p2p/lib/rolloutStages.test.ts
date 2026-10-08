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
// The stage ladder the editor accepts must be the one the gateway accepts (validate_rollout_stages),
// or an operator learns about a bad ladder from a 400 instead of from the field.
import { describe, expect, it } from "vitest";

import { DEFAULT_STAGES, formatStages, parseStages, stagesProblem } from "./rolloutStages";

describe("parseStages / formatStages", () => {
  it("reads typed text loosely and writes it back evenly", () => {
    expect(parseStages(" 1,50 %, ,100% ")).toEqual(["1", "50%", "100%"]);
    expect(formatStages(DEFAULT_STAGES)).toBe("1, 50%, 100%");
    expect(parseStages("")).toEqual([]);
  });
});

describe("stagesProblem", () => {
  it.each([
    [["1", "50%", "100%"]],
    [["1", "2", "4", "8", "100%"]],
    [["1", "2", "4", "50%", "100%"]],
    [["100%"]],
  ])("accepts %j", (stages) => {
    expect(stagesProblem(stages)).toBeNull();
  });

  it.each([
    [["1", "50%"], /last stage must be 100%/],
    [["50%", "25%", "100%"], /must rise/],
    [["4", "2", "100%"], /must rise/],
    [["1", "150%", "100%"], /more than 100%/],
    [["half", "100%"], /not a node count/],
    [["0", "100%"], /not a node count/],
    [[], /At least one stage/],
  ])("refuses %j", (stages, message) => {
    expect(stagesProblem(stages)).toMatch(message);
  });

  it("lets a campaign leave the ladder blank to inherit the cluster's", () => {
    expect(stagesProblem([], { allowEmpty: true })).toBeNull();
  });
});
