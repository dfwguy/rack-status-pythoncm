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
import type { Inventory, Preview, Readiness, RunStatus } from "../types";

// Synthetic UI fixtures. These are not cluster measurements or acceptance thresholds.
export const nodes = Array.from({ length: 8 }, (_, i) => `compute-${i + 1}`);
export const readiness: Readiness = {
  enabled: true,
  configuration_ready: true,
  execution_context_ready: true,
  errors: [],
  allowed_node_counts: [2, 4, 8],
  max_nodes: 8,
  partition: "validation",
  path_comparison: true,
  groups: [
    {
      name: "Approved pool",
      kind: "group",
      authoritative: false,
      source: "UI fixture",
      nodes,
    },
  ],
  execution_context: {
    hostname: "observer",
    user: "operator",
    kind: "local",
    cluster_name: "Lab",
    selection: { project: "lab", cluster: "lab" },
  },
};
export const inventory: Inventory = {
  captured_at: "2026-10-22T21:00:00Z",
  nodes: nodes.map((node) => ({
    node,
    states: ["IDLE"],
    partitions: ["validation"],
  })),
};
export const preview: Preview = {
  activity_id: "11111111-1111-4111-8111-111111111111",
  confirmation_token: "synthetic-preview-token",
  confirmation_expires_in_seconds: 300,
  execution_context: readiness.execution_context!,
  submission_command_display: "sbatch reviewed-static-entrypoint.sbatch",
  plan: {
    attempt_id: "11111111111141118111111111111111",
    scope: "explicit",
    node_count: 2,
    nodes: nodes.slice(0, 2),
    expected_pair_count: 1,
    expected_pairs: [nodes.slice(0, 2)],
    results_directory: "/workspace/results/attempt-11111111",
    warnings: [],
    execution_blockers: [],
    submission_enabled: true,
    effective_profile: {
      partition: "validation",
      hca: "mlx5_0:1",
      path_comparison: true,
    },
    resources: {
      cpus_per_task: 1,
      ntasks_per_node: 1,
      memory_per_node: "1G",
      gpu_resources: 0,
      exclusive: false,
      wall_seconds: 300,
    },
  },
};
const pairs = nodes.flatMap((a, i) => nodes.slice(i + 1).map((b) => [a, b]));
export const completed: RunStatus = {
  activity_id: preview.activity_id,
  scheduler_job_id: "12345",
  scheduler_state: "COMPLETED",
  phase: "done",
  work_phase: "done",
  activity: {
    activity_id: preview.activity_id,
    created_at: "2026-10-22T21:00:00Z",
    finished_at: "2026-10-22T21:01:00Z",
    phase: "done",
    stage: "pairwise",
    nodes,
    log_dir: preview.plan.results_directory,
    operator: "operator",
    node_count: 8,
    expected_pair_count: 28,
    tasks: [{ scheduler_ref: "12345" }],
  },
  summary: {
    functional_status: "PASS",
    performance_status: "UNVERIFIED",
    expected_pair_count: 28,
    nodes,
    errors: [],
    preflight: {
      status: "PASS",
      nodes: nodes.map((host, rank) => ({
        host,
        rank,
        status: "PASS",
        hca: "mlx5_0:1",
        gid_index: 3,
        architecture: "aarch64",
        link_layer: "Ethernet",
        affinity_after: [rank],
      })),
    },
    stages: Object.fromEntries(
      ["baseline", "single-path"].map((stage) => [
        stage,
        {
          exit_status: 0,
          metrics: Object.fromEntries(
            ["bandwidth", "latency"].map((metric) => [
              metric,
              {
                units: metric === "bandwidth" ? "MB/s" : "usec",
                observed_pair_count: 28,
                missing_pairs: [],
                failed_pairs: [],
                pairs: pairs.map((nodes, i) => ({
                  nodes,
                  value:
                    metric === "bandwidth" ? 22500 + i * 35 : 3.2 + i * 0.03,
                })),
              },
            ]),
          ),
        },
      ]),
    ),
  },
};
