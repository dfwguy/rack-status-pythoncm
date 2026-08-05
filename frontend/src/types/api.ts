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

/** A launched multi-rack job from the durable state table (GET /api/multirack-jobs). */
export interface MultirackJob {
  job_id: string;
  activity_id?: string;
  /** "nemo" (cross-rack NEMO validation) or "llmb" (NCCL microbenchmark). */
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
