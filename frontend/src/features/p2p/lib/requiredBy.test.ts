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

import { requiredByItems } from "./requiredBy";

const consumers = [
  { id: "nemo-26.04.01", label: "NeMo 26.04.01" },
  { id: "nemo-xrack", label: "Multirack NEMO" },
];

describe("requiredByItems", () => {
  it("offers every launch that stages a catalog image, by its exact name", () => {
    expect(requiredByItems(consumers, [])).toEqual([
      { label: "NeMo 26.04.01 (nemo-26.04.01)", value: "nemo-26.04.01" },
      { label: "Multirack NEMO (nemo-xrack)", value: "nemo-xrack" },
    ]);
  });

  it("keeps a name already on the image that this build does not know", () => {
    const items = requiredByItems(consumers, ["nemo-xrack", "retired-tool"]);
    expect(items.map((i) => i.value)).toEqual(["nemo-26.04.01", "nemo-xrack", "retired-tool"]);
    expect(items[2].label).toBe("retired-tool (unknown launch)");
  });
});
