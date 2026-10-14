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
/** A small tracker dataset in the backend's shape, for tests only. */
import type { DeploymentState, StateCounts, TrackerNode, DeploymentTrackerReport } from "../types";

function counts(partial: Partial<StateCounts>): StateCounts {
  return { completed: 0, in_progress: 0, remediation: 0, not_started: 0, handed_over: 0, ...partial };
}

function node(name: string, rack: string, state: DeploymentState, reason = ""): TrackerNode {
  const done = state === "completed" || state === "handed_over";
  return {
    node: name,
    rack,
    rack_unit: null,
    state,
    stage1: done || state === "in_progress" ? "done" : state === "remediation" ? "fail" : "pending",
    stage2: done ? "done" : "pending",
    slurm: state === "remediation" ? "IDLE+DRAIN" : "idle",
    reason,
  };
}

export const REPORT: DeploymentTrackerReport = {
  meta: {
    cluster: "Nscale NC",
    project: "Nscale",
    generated_at: "2026-10-13T12:00:00+00:00",
    snapshot_at: "2026-10-13T11:59:00Z",
    rack_mapping_mode: "translate",
    stage2_source: "bringup:llm-pretrain-solo",
    stage2_loading: false,
    states: ["completed", "in_progress", "remediation", "not_started", "handed_over"],
    definitions: {
      completed: "Both stages passed.",
      in_progress: "A stage is running.",
      remediation: "A stage failed.",
      not_started: "Neither stage has run.",
      handed_over: "Handed over.",
    },
  },
  summary: {
    total: 5,
    counts: counts({ completed: 1, in_progress: 1, remediation: 1, not_started: 1, handed_over: 1 }),
    percent_complete: 40,
    status: "in_progress",
    racks: 3,
    units: 2,
  },
  steps: [
    { id: "node_stage1", label: "Node Stage 1 · single-node NCCL", unit: "nodes", total: 4, done: 2, ongoing: 0, fail: 1, pending: 1, percent_complete: 50 },
    { id: "rack_stage1", label: "Rack Stage 1 · full-rack NCCL", unit: "racks", total: 2, done: 0, ongoing: 1, fail: 0, pending: 1, percent_complete: 0 },
  ],
  unplaced: ["appserver-nc"],
  units: [
    {
      id: "su20",
      label: "SU20",
      handed_over_at: "",
      rack_count: 2,
      total: 4,
      counts: counts({ completed: 1, in_progress: 1, remediation: 1, not_started: 1 }),
      percent_complete: 25,
      status: "in_progress",
      rows: [
        {
          key: "/a",
          label: "Row A",
          total: 2,
          counts: counts({ completed: 1, in_progress: 1 }),
          percent_complete: 50,
          status: "in_progress",
          racks: [
            {
              rack: "V20", label: "V20", stage1: "ongoing", stage2: "pending",
              total: 2, counts: counts({ completed: 1, in_progress: 1 }), percent_complete: 50, status: "in_progress",
              nodes: [node("gpu1-su20", "V20", "completed"), node("gpu2-su20", "V20", "in_progress")],
            },
          ],
        },
        {
          key: "/b",
          label: "Row B",
          total: 2,
          counts: counts({ remediation: 1, not_started: 1 }),
          percent_complete: 0,
          status: "in_progress",
          racks: [
            {
              rack: "V21", label: "V21", stage1: "pending", stage2: "pending",
              total: 2, counts: counts({ remediation: 1, not_started: 1 }), percent_complete: 0, status: "in_progress",
              nodes: [
                node("gpu3-su20", "V21", "remediation", "Stage 1 failed: fabric not up"),
                node("gpu4-su20", "V21", "not_started"),
              ],
            },
          ],
        },
      ],
    },
    {
      id: "su19",
      label: "SU19",
      handed_over_at: "2026-10-13",
      rack_count: 1,
      total: 1,
      counts: counts({ handed_over: 1 }),
      percent_complete: 100,
      status: "handed_over",
      rows: [
        {
          key: "/c",
          label: "Row C",
          total: 1,
          counts: counts({ handed_over: 1 }),
          percent_complete: 100,
          status: "handed_over",
          racks: [
            {
              rack: "U19", label: "U19", stage1: "done", stage2: "done",
              total: 1, counts: counts({ handed_over: 1 }), percent_complete: 100, status: "handed_over",
              nodes: [node("gpu1-su19", "U19", "handed_over")],
            },
          ],
        },
      ],
    },
  ],
};
