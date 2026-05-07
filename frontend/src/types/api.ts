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
