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

/**
 * One node's state for one stage (``/api/node-stages``). ``unknown`` only for Stage 2 while the
 * server is still loading the bring-up verdicts.
 */
export interface NodeStageInfo {
  state: StageState | "unknown";
  /** Slurm job that decided it (Stage 1). */
  job_id?: string;
  /** Result log that decided it (Stage 1). */
  log?: string;
  /** Bring-up verdict word, e.g. ``DONE`` / ``FAIL`` (Stage 2). */
  status?: string;
  /** Why it failed: the bring-up's reason (Stage 2), or what the result log says stopped it (Stage 1). */
  reason?: string;
  /**
   * Stage 1 only: what decided it -- ``rack-status`` (Rack Status's own single-node NCCL run) or
   * ``bringup:<job>`` (the bring-up framework's run of the same check; either pass counts).
   */
  source?: string;
}

/** Stage 1 (single-node NCCL) and Stage 2 (single-node LLM) for one node. */
export interface NodeStagesRow {
  node: string;
  rack: string;
  stage1: NodeStageInfo;
  stage2: NodeStageInfo;
}

export interface NodeStagesResponse {
  ok: boolean;
  nodes: NodeStagesRow[];
  /** Where a node's Stage 2 comes from, e.g. ``bringup:llm-pretrain-solo``. */
  stage2_source: string;
  /** What may decide Stage 1, e.g. ``["rack-status", "bringup:allreduce"]``. */
  stage1_sources?: string[];
  stage2_loading: boolean;
  stage2_error?: string;
  error?: string;
}

export interface RackRow {
  rack: string;
  nemo: string;
  /** Stage 1 — NCCL benchmark. Optional (legacy shell/multirack rows omit it). */
  stage1?: StageState;
  /** Stage 2 — NeMo / LLM training. Optional (legacy shell/multirack rows omit it). */
  stage2?: StageState;
  last_job: string;
  sinfo: string;
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

/**
 * One Scalable Unit of the active cluster, and the racks of this snapshot in it.
 *
 * Sent by the backend from the cluster record rather than derived here, because an SU map
 * is a site fact. The implementation this replaced carried a fourteen-entry row-to-SU
 * table hard-coded in TypeScript — correct for one customer's floor and silently wrong
 * everywhere else.
 */
export interface StatusScalableUnit {
  id: string;
  label: string;
  racks: string[];
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
  /** Evidence source for work_phase: squeue|scontrol|sacct|results_log|missing */
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
  /** Wrapper/launch lifecycle (not Slurm work completion). */
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
  /** The node's rack as the cluster's mapping places it; "" for a pool cluster or an unplaced node. */
  rack: string;
  /** Position in the rack as the cluster manager places it; absent when not placed. */
  rack_unit?: string;
  tray?: number | null;
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

/**
 * One card of the post-run history. A rack on a `direct`/`translate` cluster, the single pool on a
 * `pool` cluster (no placement exists), or the nodes the mapping could not place. Fields beyond
 * `rack`/`nodes` are absent from a backend that predates placement-aware grouping.
 */
export interface PostRunGroup {
  rack: string;
  label?: string;
  kind?: "rack" | "pool" | "unplaced";
  scalable_unit?: { id: string; label: string } | null;
  nodes: PostRunNode[];
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
  racks: PostRunGroup[];
  nodes: PostRunNode[];
  rack_mapping_mode?: "direct" | "translate" | "pool";
  /** Why nodes are shown unplaced (snapshot still loading or unavailable); "" when placement is known. */
  placement_error?: string;
  refresh: {
    activities: number;
    merge: Record<string, unknown>;
    accounting_error: string;
    inventory_error: string;
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

/**
 * What the workload manager says a node can do now, in words every scheduler maps into
 * (``schedulers.base.NODE_STATES``). The server maps the raw state -- Slurm's ``drng``, ``mix-``,
 * ``IDLE+DRAIN`` -- so no view has to know whose vocabulary it is reading.
 */
export type SchedulerNodeStatus =
  | "idle"
  | "running"
  | "pending"
  | "reserved"
  | "draining"
  | "drained"
  | "down"
  | "unknown";

/** Discovered compute host; rack membership is optional inventory metadata. */
export interface NodeInventoryEntry {
  id: string;
  rack: string | null;
  /** The cluster manager's view (BCM ``ds``): powered and reachable, installing, or down. */
  status: "up" | "down" | "inst" | "unknown";
  ip: string;
  /** The scheduler's raw state, as it spelled it (Slurm ``drain``, ``alloc``, ``mix-``). */
  scheduler_state?: string;
  /** ``scheduler_state`` in the shared vocabulary. Absent on older servers. */
  scheduler_status?: SchedulerNodeStatus;
  /** Position in the rack as the cluster manager places it (BCM ``U20:11`` -> ``"11"``). Present only
   * for a node that resolved to a rack and whose provider reports a unit; never derived from the
   * hostname. */
  rack_unit?: string;
  /** Tray number 1..N within the rack: the node's place in natural hostname order (the site's
   * install order), numbered by the server so every view agrees. Absent for an unplaced node. */
  tray?: number;
  /** GPUs on this node, from the scheduler (Slurm GRES) or an nvidia-smi sample. Shape is
   * orthogonal to placement: a pool-mode node with no rack still carries a count. Absent = unknown. */
  gpu_count?: number;
  /** GPU model label when known (e.g. "nvidia", "NVIDIA RTX 6000 Ada Generation"). */
  gpu_type?: string;
}

export interface NodeSources {
  orchestrator: string;
  scheduler: string;
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
  /** Discovered hosts; null means unavailable, while absence identifies older servers. */
  nodes?: NodeInventoryEntry[] | null;
  /** Discovery failure, including whether previously discovered nodes are being retained. */
  node_inventory_error?: string;
  /**
   * Who answered for each half of a node record: ``orchestrator`` for ``status`` (``bcm``, ``maas``),
   * ``scheduler`` for ``scheduler_status`` (``slurm``; "" when no scheduler answered). Absent on
   * older servers.
   */
  node_sources?: NodeSources;
  summary: RackSummary;
  footnotes: string[];
  comments: Record<string, string>;
  physical_rf0: Record<string, string>;
  /** Tray / validation naming from ``NVIS_RACK_ID`` / ``NVIS_CT_ID`` (see Settings). */
  naming?: NvisNamingSchema;
  /** Section -> row -> rack tree derived from the naming module (floor view, etc.). */
  hierarchy?: RackHierarchy;
  /**
   * This cluster's SU grouping, when it has one. Absent or empty means "no SU model" and
   * the floor view keeps drawing rows — it does not mean one SU holding every rack.
   */
  scalable_units?: StatusScalableUnit[];
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
  /** How nodes map to the floor: "direct" / "translate" place on racks; "pool" has no placement
   * (flat node list). Absent on older servers — treat as "direct". See catalog Cluster. */
  rack_mapping_mode?: "direct" | "translate" | "pool";
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
}

export interface WorkerStatus {
  id: string;
  label: string;
  thread_alive: boolean;
  finished_once: boolean;
}

/** Slurm squeue snapshot embedded in cluster overview (head / controller only). */
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

/** GET /api/cluster-overview — cluster-wide facts (env + head-only Slurm; no compute-node SSH/pdsh). */
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

/** GET /api/cluster-overview/live — independent BCM / Slurm / queue probes. */
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
