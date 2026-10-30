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
import type { NodeInventoryEntry, NodeSources, RackHierarchy, StatusScalableUnit } from "../../types/api";
import type { NvisNamingSchema } from "../../types/naming";

export interface ExecutionContext {
  hostname: string;
  user: string;
  kind: string;
  cluster_name: string;
  selection: { project: string; cluster: string };
}

export interface Readiness {
  enabled: boolean;
  configuration_ready: boolean;
  execution_context_ready: boolean;
  errors: string[];
  execution_context_errors?: string[];
  disabled_reason?: string;
  partition?: string;
  path_comparison?: boolean;
  regex_selection_enabled?: boolean;
  allowed_node_counts: number[];
  max_nodes: number | null;
  rack_inventory_enabled?: boolean;
  whole_rack_enabled?: boolean;
  rack_inventory_ready?: boolean;
  rack_inventory_error?: string;
  groups?: {
    name: string;
    kind: string;
    authoritative: boolean;
    nodes: string[];
    source: string;
  }[];
  execution_context?: ExecutionContext;
}

export interface InventoryNode {
  node: string;
  allowed?: boolean;
  states: string[];
  partitions: string[];
}
export interface Inventory {
  group?: string;
  nodes: InventoryNode[];
  captured_at: string | null;
  note?: string;
}
export interface RunRequest {
  scope: "preflight" | "explicit" | "group" | "rack";
  nodes?: string[];
  group?: string;
  node_count: number;
  path_comparison?: boolean;
}
export interface Preview {
  activity_id: string;
  confirmation_token: string;
  confirmation_expires_in_seconds: number;
  execution_context: ExecutionContext;
  submission_command_display: string | null;
  plan: {
    attempt_id: string;
    scope: string;
    node_count: number;
    nodes: string[];
    expected_pair_count: number;
    expected_pairs: string[][];
    results_directory: string;
    warnings: string[];
    execution_blockers: string[];
    submission_enabled: boolean;
    effective_profile: {
      partition: string;
      hca: string;
      path_comparison: boolean;
      [key: string]: unknown;
    };
    resources: {
      cpus_per_task: number;
      ntasks_per_node: number;
      memory_per_node: string;
      gpu_resources: number;
      exclusive: boolean;
      wall_seconds: number;
    };
  };
}
export interface PairMetric {
  units: string;
  observed_pair_count: number;
  missing_pairs: string[][];
  failed_pairs: unknown[];
  pairs: { nodes: string[]; value: number; status?: string }[];
}
export interface NodeDiagnostic {
  rank: number;
  host: string;
  status: string;
  hca?: string;
  gid_index?: number;
  architecture?: string;
  link_layer?: string;
  affinity_after?: number[];
  errors?: string[];
  error?: string;
  global_v2_gid?: { gid: string };
}
export interface StageFailure {
  category: string;
  message: string;
  // Historical backend field: nodes reporting the log error, not proven faulty hardware.
  affected_nodes?: string[];
  evidence?: { artifact: string; line?: number; excerpt: string }[];
}
export interface Summary {
  functional_status: string;
  performance_status: string;
  expected_pair_count: number;
  nodes: string[];
  errors: string[];
  warnings?: string[];
  preflight?: { status: string; nodes?: NodeDiagnostic[]; exit_status?: number | null };
  clusterkit_exit_status?: Record<string, number>;
  stages?: Record<
    string,
    { exit_status: number | null; status?: "PASS" | "FAIL" | "SKIPPED"; reason?: string; failure?: StageFailure | null; metrics?: Record<string, PairMetric> }
  >;
}
export interface Activity {
  activity_id: string;
  created_at: string;
  finished_at?: string;
  phase: string;
  work_phase?: string;
  stage: string;
  nodes: string[];
  log_dir: string;
  operator: string;
  error?: string;
  tasks?: { scheduler_ref?: string; scheduler_state?: string }[];
  node_count?: number;
  expected_pair_count?: number;
}
export interface RunStatus {
  activity_id: string;
  scheduler_job_id: string | null;
  scheduler_state: string;
  phase: string;
  work_phase: string;
  error?: string;
  scheduler_error?: string;
  summary_error?: string;
  summary: Summary | null;
  stdout_tail?: string;
  stderr_tail?: string;
  command_display?: string;
  activity: Activity;
}
export interface Artifact {
  name: string;
  downloadable?: boolean;
  size?: number;
  bytes?: number;
}
export interface HistoryPage {
  runs: Activity[];
  next_cursor: string | null;
}

export interface RackDirectoryEntry {
  whole_rack_enabled?: boolean;
  whole_rack_reason?: string;
  name: string;
  node_count: number;
  nodes: string[];
  counts: { idle: number; busy: number; drain: number; down: number; reserved: number; unknown: number };
  allowed_node_count: number;
  allowed_node_counts: number[];
  authoritative: boolean;
  test_enabled: boolean;
  reason: string;
}
export interface RackDirectory {
  racks: RackDirectoryEntry[];
  nodes: NodeInventoryEntry[];
  snapshot: { state: string; source?: string; age_seconds: number | null; last_success_at?: string | null; checkpoint_only?: boolean };
  node_sources: NodeSources;
  unplaced_node_count: number;
  error?: string;
  naming?: NvisNamingSchema | null;
  hierarchy?: RackHierarchy | null;
  scalable_units?: StatusScalableUnit[];
}
