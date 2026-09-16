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
/** Per-stage validation state. A rack is individually validated once stage 1 & 2 are both "done". */
export type StageState = "done" | "fail" | "ongoing" | "pending";

export interface RackRow {
  rack: string;
  nemo: string;
  /** Stage 1 — NCCL benchmark. Optional (legacy shell/multirack rows omit it). */
  stage1?: StageState;
  /** Stage 2 — NeMo / LLM training. Optional (legacy shell/multirack rows omit it). */
  stage2?: StageState;
  last_job: string;
  sinfo: string;
  /** Source used for the availability/health verdict; Slurm is authoritative when present. */
  availability_source?: "slurm" | "pythoncm" | string;
  ct_down_unk: string;
  /** Why pending / fail / ongoing (from rack_status.sh or native); omit when done or unknown. */
  nemo_hint?: string;
}

import type { NvisNamingSchema } from "./naming";

/** A rack within the backend-derived hierarchy (id + short display label). */
export interface HierarchyRack {
  rack: string;
  label: string;
}

/** A datacenter row (or pod) within a section. */
export interface HierarchyRow {
  key: string;
  label: string;
  /** BCM NodeGroup for SU labels; rack-id-template for coordinate-derived labels. */
  label_source?: "pythoncm-node-group" | "rack-id-template";
  racks: HierarchyRack[];
}

/** A datacenter section (hall); ``key`` is "" for sites without a section level. */
export interface HierarchySection {
  key: string;
  label: string;
  rows: HierarchyRow[];
}

/**
 * Backend-authoritative cluster topology (section -> row -> rack) built from the
 * active ``NVIS_RACK_ID`` naming. Single source of truth for layout: the SPA
 * consumes this instead of re-parsing rack ids. ``levels`` lists the meaningful
 * grouping levels (``section`` omitted for 2-slot templates).
 */
export interface RackHierarchy {
  levels: string[];
  sections: HierarchySection[];
  unmatched: string[];
}

/** Child scheduler task on a validation activity (NCCL then NeMo, etc.). */
export interface ActivityTask {
  task_id: string;
  ordinal: number;
  label: string;
  scheduler_ref?: string | null;
  scheduler_state?: string | null;
  phase?: string;
  /** Work lifecycle: pending|submitted|running|collecting|done|failed|unknown */
  work_phase?: string | null;
  /** Evidence source for work_phase. Legacy payloads may name Slurm commands; PythonCM/direct uses provider-neutral values. */
  evidence?: string | null;
  stdout_path?: string | null;
  training_log_path?: string | null;
  results_log_path?: string | null;
  /** When the task entered "collecting"; the backend resolves it terminally after a grace period. */
  collecting_since?: string | null;
  updated_at?: string;
}

/** Durable validation activity (GET /api/validation-activities). */
export interface ValidationActivity {
  activity_id: string;
  job_id: string;
  tool: string;
  kind: string;
  stage: string;
  scope: string;
  rack: string;
  racks: string[];
  log_dir: string;
  operator: string;
  command_display: string;
  scheduler: string;
  dry_run: boolean;
  /** Wrapper/launch lifecycle (not scheduler work completion). */
  phase: string;
  /** Rolled-up work lifecycle across tasks[]. */
  work_phase?: string | null;
  returncode: number | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  stdout_tail: string;
  stderr_tail: string;
  source: string;
  tasks: ActivityTask[];
}

export interface ValidationActivitiesResponse {
  ok: boolean;
  activities: ValidationActivity[];
  error?: string;
}

export interface NodeIssue {
  issue_id: string;
  node: string;
  rack: string;
  category: "hardware" | "network" | string;
  code: string;
  reason: string;
  severity: string;
  source: string;
  validation_run_id: string;
  scheduler_ref: string;
  evidence: string;
  drained: boolean;
  slurm_reason: string;
  drain_result: string;
  operator: string;
  created_at: string;
}

export interface NodeIssueRollup {
  node: string;
  rack: string;
  issue_count: number;
  imported_count: number;
  local_count: number;
  drained: boolean;
  nccl: boolean;
  maintenance: boolean;
  latest: NodeIssue | null;
}

export interface NodeIssuesResponse {
  ok: boolean;
  error?: string;
  sync_error?: string;
  updated_at: string;
  punchcard_synced_at: string;
  summary: {
    nodes_tracked: number;
    nodes_with_issues: number;
    currently_flagged_drained: number;
    repeat_nodes: number;
    local_findings: number;
    hardware_findings: number;
    network_findings: number;
  };
  nodes: Record<string, NodeIssueRollup>;
  issues: NodeIssue[];
}

export interface DeepAuditNodeResult {
  node: string;
  status: "pass" | "warning" | "fail" | "inconclusive";
  codes?: string[];
  reason?: string;
  warnings?: string[];
  metrics?: Record<string, unknown>;
  scheduler_ref?: string;
  drained: boolean;
  slurm_reason?: string;
  drain_result?: string;
}

export interface DeepAuditSummary {
  kind: string;
  total: number;
  passed: number;
  warning: number;
  failed: number;
  inconclusive: number;
  drained: number;
  drain_failed: number;
  nodes: DeepAuditNodeResult[];
}

export interface DeepAuditRun {
  run_id: string;
  workflow_id: string;
  workflow_name: string;
  state: string;
  rack: string;
  target: string;
  started_at: string;
  updated_at: string;
  audits: Record<string, DeepAuditSummary>;
  error?: string;
}

export interface PostRunEvent {
  node: string;
  rack: string;
  job_id: string;
  activity_id: string;
  tool: string;
  scope: string;
  state: string;
  started_at: string;
  ended_at: string;
  exit_code: string;
  failed_job: boolean;
  attributed_failure: boolean;
  category: string;
  reason: string;
  source: string;
  output_run_id?: string;
  output_step_id?: string;
  validation_test?: string;
  raw_test?: string;
  authoritative?: boolean;
  history_only?: boolean;
  artifact_path?: string;
  evidence_timestamp?: string;
  observed_at?: string;
  map_method?: string;
}

export interface PostRunAuditStatus extends DeepAuditNodeResult {
  kind: "hardware" | "network" | string;
  rack: string;
  run_id: string;
  started_at: string;
  updated_at: string;
}

export interface PostRunNode {
  node: string;
  rack: string;
  slurm_state: string;
  slurm_reason: string;
  participation_count: number;
  failed_job_participations: number;
  failure_count: number;
  distinct_failure_runs: number;
  flagged: boolean;
  trend: "up" | "down" | "flat" | "new";
  last_run_at: string;
  last_failure_at: string;
  runs: PostRunEvent[];
  findings: NodeIssue[];
  hardware_status: PostRunAuditStatus | null;
  network_status: PostRunAuditStatus | null;
}

export interface PostRunNodeReportResponse {
  ok: boolean;
  generated_at: string;
  history_path: string;
  summary: {
    nodes: number;
    participations: number;
    attributed_failures: number;
    flagged_nodes: number;
    drained_nodes: number;
  };
  racks?: Array<{ rack: string; nodes: PostRunNode[] }>;
  nodes: PostRunNode[];
  compact?: boolean;
  refresh: {
    activities: number;
    merge: Record<string, unknown>;
    accounting_error: string;
    inventory_error: string;
    dcgm_error?: string;
    dcgm_events?: number;
    dcgm_observed_at?: string;
    healthcheck_error?: string;
    healthcheck_events?: number;
    healthcheck_observed_at?: string;
  };
}

/** One durable fleet report saved as soon as background generation completes. */
export interface GeneratedReport {
  id: string;
  kind: string;
  format: "csv" | "json" | "pdf";
  filename: string;
  size: number;
  /** Unix epoch seconds. */
  created: number;
  cluster?: string;
  nodes?: number;
  schema_errors?: number;
}

export interface GeneratedReportsResponse {
  ok: boolean;
  reports: GeneratedReport[];
  max_kept: number;
  error?: string;
}

/** A launched multi-rack job from the durable state table (GET /api/multirack-jobs). */
export interface MultirackJob {
  job_id: string;
  activity_id?: string;
  /** Cross-rack NEMO, NCCL microbenchmark, or Llama Exemplar launch kind. */
  kind: string;
  tool?: string;
  stage?: string;
  scope?: string;
  rack?: string;
  racks: string[];
  log_dir: string;
  operator: string;
  command_display: string;
  dry_run: boolean;
  /** starting | running | done | error | timeout. */
  phase: string;
  returncode: number | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  stdout_tail: string;
  stderr_tail: string;
  tasks?: ActivityTask[];
}

export interface MultirackJobsResponse {
  ok: boolean;
  jobs: MultirackJob[];
  error?: string;
}

export interface RackStatusResponse {
  ok: boolean;
  returncode: number;
  updated_at: string | null;
  log_dir: string;
  script: string;
  stderr: string;
  legend: string;
  rows: RackRow[];
  summary: RackSummary;
  footnotes: string[];
  comments: Record<string, string>;
  physical_rf0: Record<string, string>;
  /** Tray / validation naming from ``NVIS_RACK_ID`` / ``NVIS_CT_ID`` (see Settings). */
  naming?: NvisNamingSchema;
  /** Section -> row -> rack tree derived from the naming module (floor view, etc.). */
  hierarchy?: RackHierarchy;
  native_workers: NativeWorkers;
  served_from_cache?: boolean;
  rack_status_background_interval_sec?: number;
  /** From ``RACK_WEB_PROJECT_NAME`` / ``PROJECT_NAME`` (optional title / customer fallback). */
  project_name?: string;
  /** False when the head node is not reachable over SSH (e.g. VPN drop). */
  cluster_reachable?: boolean;
  cluster_unreachable_detail?: string;
  /** True when the backend returned a fast placeholder because the link is down. */
  degraded?: boolean;
  exec_mode?: "ssh" | "local";
  snapshot: ClusterSnapshotMeta;
}

/** Count of racks in each state for a single validation stage. */
export interface StageSummary {
  done: number;
  fail: number;
  ongoing: number;
  pending: number;
}

export interface RackSummary {
  done: number;
  fail: number;
  ongoing: number;
  pending: number;
  /** Per-stage rack counts (native / demo backends). Absent on legacy shell table. */
  stage1?: StageSummary;
  stage2?: StageSummary;
}

export interface NativeWorkers {
  mode: string;
  note?: string;
  rack_count?: number;
  workers?: WorkerStatus[];
  availability?: AvailabilitySummary;
}

export interface NodeDownSheetSummary {
  ok: boolean;
  tab?: string;
  updated_at?: string;
  unavailable?: number | null;
  responding?: number | null;
  no_response?: number | null;
  drained?: number | null;
  down_other?: number | null;
  delta?: number;
  matches_live?: boolean;
  error?: string;
}

export interface AvailabilitySummary {
  source: string;
  captured_at: string;
  total_nodes: number;
  ready_nodes: number;
  unavailable_nodes: number;
  no_response_nodes: number;
  drained_nodes: number;
  down_other_nodes: number;
  invalid_nodes: number;
  definition: string;
  sheet?: NodeDownSheetSummary;
}

export interface WorkerStatus {
  id: string;
  label: string;
  thread_alive: boolean;
  finished_once: boolean;
}

/** Active-job snapshot embedded in cluster overview. The field name remains Slurm-shaped for API compatibility. */
export interface ClusterSqueueBlock {
  ok: boolean;
  stdout: string;
  stderr?: string;
  command_display?: string;
  returncode?: number;
  error?: string;
}

/** Values persisted via PUT /api/cluster-dashboard-metadata (fill gaps when env is unset). */
export interface ClusterDashboardMetadata {
  product_line: string;
  hw_vendor: string;
  customer_name: string;
}

export type ClusterSnapshotState = "initializing" | "fresh" | "stale" | "failed";

/** Canonical cross-process inventory snapshot metadata returned by both inventory APIs. */
export interface ClusterSnapshotMeta {
  id: string | null;
  state: ClusterSnapshotState;
  cluster_id: string;
  log_root: string;
  configuration_generation: number;
  last_attempt_at: string | null;
  last_success_at: string | null;
  age_seconds: number | null;
  last_error: string | null;
}

/** GET /api/cluster-overview — cluster-wide facts and controller-side scheduler data. */
export interface ClusterOverviewResponse {
  ok: true;
  snapshot: ClusterSnapshotMeta;
  cluster_name: string;
  customer_name: string;
  product_line: string;
  hw_vendor: string;
  bcm_version: string;
  slurm_version: string;
  nemo_container: string;
  /** Parent dir of NEMO_SQSH_SRC when it ends with .sqsh; else expanded path. Empty if unset. */
  tools_download_destination_default?: string;
  /** Effective workspace root (NVIS_WORKSPACE_DIR) for HPC-X / CUDA / nvloom installs. */
  tools_workspace_default?: string;
  /** Compute hostnames from rack status ``physical_rf0`` keys (suggestions for CUDA/NCCL pdsh build host). */
  cuda_nccl_build_host_candidates?: string[];
  rack_count: number | null;
  tray_hosts: number | null;
  estimated_gpu_count: number | null;
  validation_summary: RackSummary | null;
  validation_cells_total: number | null;
  status_updated_at: string | null;
  probes: Record<string, unknown>;
  app_version: string;
  connected_users: number;
  squeue: ClusterSqueueBlock;
  cluster_dashboard_metadata: ClusterDashboardMetadata;
  /**
   * Head-node reachability from the base overview route.
   * ``null`` when the backend has no cached reachability yet (unknown).
   */
  cluster_reachable?: boolean | null;
  cluster_unreachable_detail?: string;
  exec_mode?: "ssh" | "local";
}

/** Per-section state for progressive `/api/cluster-overview/live` probes. */
export type LiveProbeState = "initializing" | "fresh" | "stale" | "failed";

export interface LiveProbeSection<T = unknown> {
  state: LiveProbeState;
  value: T | null;
  last_attempt_at: string | null;
  last_success_at: string | null;
  age_seconds: number | null;
  last_error: string | null;
}

export interface ClusterOverviewLiveSections {
  bcm: LiveProbeSection<string>;
  slurm: LiveProbeSection<string>;
  queue: LiveProbeSection<ClusterSqueueBlock>;
}

/** GET /api/cluster-overview/live — independent cluster-manager / scheduler / queue probes. */
export interface ClusterOverviewLiveResponse {
  ok: true;
  cluster_id: string;
  reachable: boolean;
  unreachable_detail: string;
  sections: ClusterOverviewLiveSections;
}

/** GET /api/cluster-health — cheap cached reachability for the connection banner. */
export interface ClusterHealthResponse {
  ok: boolean;
  mode: "ssh" | "local";
  reachable: boolean;
  detail?: string;
  host?: string | null;
  checked_at?: number | null;
  ttl_sec?: number;
  error?: string;
}
