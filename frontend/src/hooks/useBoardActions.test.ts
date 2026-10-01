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
import { act, renderHook, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { useBoardActions } from "./useBoardActions";
import type { Tool } from "../types/catalog";

const { active, listToolsMock } = vi.hoisted(() => ({
  active: { activeCluster: { id: "nvlink-cluster" }, version: 0 },
  listToolsMock: vi.fn(),
}));
vi.mock("../context/ActiveClusterContext", () => ({ useActiveCluster: () => active }));
vi.mock("../services/catalogService", () => ({ listTools: listToolsMock }));
vi.mock("../services/workflowService", () => ({ listWorkflows: async () => [] }));

it("withholds the previous cluster's tools until the new catalog arrives", async () => {
  const preflight: Tool = {
    id: "nvlink-preflight", name: "NVLink Preflight", surfaces: [],
    category: "validation", script: "nvlink_preflight.sh", env_key: "RACK_NVLINK_PREFLIGHT_SCRIPT",
    params: [], compatible: { match: "all", capabilities: [], platform_ids: [] },
    execution: { mode: "async", target: "rack" },
    description: "", ai_context: "", status: "ga", source: "seed", subtitle: "",
    button_label: "Run", icon: "", image: "", destructive: false, order: 10,
    launch: {
      kind: "async-job", endpoint: "/api/nvlink-preflight-async", preset_id: "", dialog: "",
      scopes: [], body: {}, confirm: false, min_nodes: 0, max_nodes: 0,
    },
  };
  listToolsMock.mockResolvedValueOnce([preflight]);
  let finish!: (tools: Tool[]) => void;
  listToolsMock.mockImplementationOnce(() => new Promise<Tool[]>((resolve) => { finish = resolve; }));
  const observed: string[][] = [];
  const { result, rerender } = renderHook(() => {
    const state = useBoardActions();
    observed.push(state.actions.map((action) => action.id));
    return state;
  });
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.actions[0].id).toBe("nvlink-preflight");

  active.activeCluster = { id: "rtx-cluster" };
  active.version += 1;
  observed.length = 0;
  rerender();
  expect(observed.every((ids) => ids.length === 0)).toBe(true);
  expect(result.current.loading).toBe(true);

  await act(async () => { finish([]); });
  expect(result.current.loading).toBe(false);
  expect(result.current.actions).toEqual([]);
});
