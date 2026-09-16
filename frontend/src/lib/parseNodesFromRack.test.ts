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
import { parseNodesFromRack } from "./parseNodesFromRack";

const NSCALE_NAMING = {
  rack_id: "ink50-dh[01-99]-[a-z][\\d+]",
  ct_id: "ink50-dh[01-99]-[a-z][\\d+]-[\\d+]",
  nvsw_id: "ink50-dh[01-99]-[a-z][\\d+]-[\\d+]-nv",
  ct_joiner: "-",
  node_id_mode: "full_hostname" as const,
};

describe("parseNodesFromRack Nscale PythonCM identity", () => {
  it("keeps live UP/DOWN state when physical inventory is present", () => {
    const nodes = parseNodesFromRack(
      "ink50-dh01-a01",
      "DN:ink50-dh01-a01-[18];UP:ink50-dh01-a01-[11-17,28-37]",
      {
        "ink50-dh01-a01-11": "10.41.172.2",
        "ink50-dh01-a01-18": "10.41.172.9",
      },
      18,
      NSCALE_NAMING,
    );

    expect(nodes.find((node) => node.id === "ink50-dh01-a01-11")).toMatchObject({
      status: "up",
      ip: "10.41.172.2",
    });
    expect(nodes.find((node) => node.id === "ink50-dh01-a01-18")).toMatchObject({
      status: "down",
      ip: "10.41.172.9",
    });
    expect(nodes).toHaveLength(18);
  });

  it("preserves the hostname joiner while the physical inventory catches up", () => {
    const nodes = parseNodesFromRack(
      "ink50-dh01-a01",
      "UP:ink50-dh01-a01-[11-12]",
      {},
      18,
      NSCALE_NAMING,
    );

    expect(nodes.map((node) => node.id)).toEqual([
      "ink50-dh01-a01-11",
      "ink50-dh01-a01-12",
    ]);
    expect(nodes.every((node) => node.status === "up")).toBe(true);
  });
});
