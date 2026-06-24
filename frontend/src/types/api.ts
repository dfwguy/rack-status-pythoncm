export interface RackRow {
  rack: string;
  nemo: string;
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

export interface RackStatusResponse {
  ok: boolean;
  returncode: number;
  updated_at: string;
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
  /** From ``RACK_WEB_PROJECT_NAME`` / ``PROJECT_NAME`` (sidebar brand when set). */
  project_name?: string;
  /** False when the head node is not reachable over SSH (e.g. VPN drop). */
  cluster_reachable?: boolean;
  cluster_unreachable_detail?: string;
  /** True when the backend returned a fast placeholder because the link is down. */
  degraded?: boolean;
  exec_mode?: "ssh" | "local";
}

export interface RackSummary {
  done: number;
  fail: number;
  ongoing: number;
  pending: number;
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

/** GET /api/cluster-overview — cluster-wide facts (env + head-only Slurm; no compute-node SSH/pdsh). */
export interface ClusterOverviewResponse {
  ok: true;
  cluster_name: string;
  customer_name: string;
  product_line: string;
  hw_vendor: string;
  bcm_version: string;
  slurm_version: string;
  nemo_container: string;
  /** Parent dir of NEMO_SQSH_SRC when it ends with .sqsh; else expanded path. Empty if unset. */
  tools_download_destination_default?: string;
  /** Compute hostnames from rack status ``physical_rf0`` keys (suggestions for CUDA/NCCL pdsh build host). */
  cuda_nccl_build_host_candidates?: string[];
  rack_count: number;
  tray_hosts: number;
  estimated_gpu_count: number;
  validation_summary: RackSummary;
  validation_cells_total: number;
  status_updated_at: string | null;
  probes: Record<string, unknown>;
  app_version: string;
  connected_users: number;
  squeue: ClusterSqueueBlock;
  cluster_dashboard_metadata: ClusterDashboardMetadata;
  /** False when the head node is not reachable over SSH (e.g. VPN drop). */
  cluster_reachable?: boolean;
  cluster_unreachable_detail?: string;
  exec_mode?: "ssh" | "local";
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
