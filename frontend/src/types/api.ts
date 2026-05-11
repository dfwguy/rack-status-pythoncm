export interface RackRow {
  rack: string;
  nemo: string;
  last_job: string;
  sinfo: string;
  ct_down_unk: string;
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
  native_workers: NativeWorkers;
  served_from_cache?: boolean;
  rack_status_background_interval_sec?: number;
  /** From ``RACK_WEB_PROJECT_NAME`` / ``PROJECT_NAME`` (sidebar brand when set). */
  project_name?: string;
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
}
