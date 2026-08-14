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

/** One node in the fleet feed. Same job shape as a node summary, minus the per-node probe. */
export interface BringupFleetNode {
  node: string;
  slurm_state: string;
  jobs: BringupJob[];
}

/**
 * Fleet-wide bring-up state from GET /api/bringup/fleet — every node in one response, which
 * is what a cluster heatmap and the daily report need.
 */
export interface BringupFleet {
  home: string;
  /** Active benchmark columns, in the order the bring-up report defines them. */
  columns: string[];
  /** Metric label per column, for columns that produce a number. */
  metric_labels: Record<string, string>;
  nodes: BringupFleetNode[];
}
