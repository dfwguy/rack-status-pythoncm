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
 * The deployment tracker dataset (`GET /api/deployment-tracker`, `backend/deployment_tracker.py`).
 *
 * The same shape reaches the in-app view and the static page published from `site.html`, so the
 * two can never disagree about what a state means: the definitions travel in `meta`.
 */
import type { StageState } from "../../types/api";

export type DeploymentState = "completed" | "in_progress" | "remediation" | "not_started" | "handed_over";

export type StateCounts = Record<DeploymentState, number>;

/** What every grouping level carries: its counts and the roll-up of them. */
export interface TrackerRollup {
  total: number;
  counts: StateCounts;
  percent_complete: number;
  /** A group's overall status. Remediation is never a group status; it shows in the counts. */
  status: Exclude<DeploymentState, "remediation">;
}

export interface TrackerNode {
  node: string;
  rack: string;
  rack_unit?: number | null;
  tray?: number | null;
  state: DeploymentState;
  stage1: StageState;
  stage2: StageState;
  /** Raw Slurm state, e.g. `IDLE+DRAIN`. */
  slurm: string;
  /** Why a node in remediation is there; empty otherwise. */
  reason: string;
}

export interface TrackerRack extends TrackerRollup {
  rack: string;
  label: string;
  /** The full-rack stages, from the rack table. */
  stage1: StageState;
  stage2: StageState;
  nodes: TrackerNode[];
}

export interface TrackerRow extends TrackerRollup {
  key: string;
  label: string;
  racks: TrackerRack[];
}

export interface TrackerUnit extends TrackerRollup {
  id: string;
  label: string;
  /** ISO date the SU was handed over to the customer; empty while it is still ours. */
  handed_over_at: string;
  rack_count: number;
  rows: TrackerRow[];
}

export interface TrackerStep {
  id: string;
  label: string;
  unit: "nodes" | "racks";
  total: number;
  done: number;
  ongoing: number;
  fail: number;
  pending: number;
  percent_complete: number;
}

export interface DeploymentTrackerReport {
  meta: {
    cluster: string;
    project: string;
    generated_at: string;
    snapshot_at: string;
    rack_mapping_mode: string;
    stage2_source: string;
    stage2_loading: boolean;
    states: DeploymentState[];
    definitions: Record<DeploymentState, string>;
  };
  summary: TrackerRollup & { racks: number; units: number };
  steps: TrackerStep[];
  units: TrackerUnit[];
  /** Hosts in the inventory that no rack holds (e.g. the app server); counted nowhere, named here. */
  unplaced: string[];
}

export interface DeploymentTrackerResponse extends DeploymentTrackerReport {
  ok: true;
}
