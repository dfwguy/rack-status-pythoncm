/**
 * Types for the per-node bring-up results feed (Travis's automation under
 * /home/cmods/bringup). Mirrors the payload from GET /api/bringup/node and
 * /api/bringup/config (see backend/bringup.py).
 */

/** One benchmark column for a node: authoritative status + optional metric. */
export interface BringupJob {
  /** Column name (job, or `<job>-runN` for multi-run jobs). */
  name: string;
  /** Base job name with any run suffix stripped (drives metric label + charts). */
  base: string;
  /** DONE / FAIL / RETRY / INCOMPLETE / RUNNING / NOT_RUN (or a raw cell word). */
  status: string;
  /** Failure/context reason from status-check.sh; empty for DONE/NOT_RUN. */
  reason: string;
  /** Extracted metric value (string to preserve precision), or null. */
  metric: string | null;
  /** Human label for the metric (e.g. "busbw GB/s"), or null when the job has none. */
  metric_label: string | null;
}

export interface BringupNodeSummary {
  node: string;
  present: boolean;
  slurm_state: string;
  home: string;
  jobs: BringupJob[];
}

export interface BringupConfig {
  enabled: boolean;
  present: boolean;
  home: string;
}
