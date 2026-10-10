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
// Pull rollouts (/api/p2p/rollouts): one catalog image onto racks through the node agents'
// desired-state documents, rung by rung, read back from the agents' metrics.

/** How a rollout paces itself; editable while it runs. */
export interface RolloutPacing {
  stages: string[];
  budget_seconds: number;
  max_active_racks: number;
}

export type RolloutNodeState = "pending" | "released" | "downloading" | "ready" | "deferred";

export interface RolloutCounts {
  total: number;
  pending: number;
  released: number;
  downloading: number;
  ready: number;
  deferred: number;
}

/** A row of GET /api/p2p/rollouts. The run state is the workflow engine's. */
export interface Rollout {
  run_id: string;
  state: "pending" | "running" | "completed" | "failed" | "cancelled" | string;
  started_at: string;
  updated_at: string;
  operator: string;
  cluster_id: string;
  image: string;
  racks: string[];
  pacing: RolloutPacing;
  counts: RolloutCounts;
  last_error: string;
}

export interface RolloutNode {
  node: string;
  state: RolloutNodeState;
  reason: string;
  rung: number;
  released_at: number;
  ready_at: number;
  attempts: number;
}

export interface RolloutRack {
  rack: string;
  state: "waiting" | "active" | "done";
  rung: number;
  cuts: number[];
  nodes: RolloutNode[];
}

/** GET /api/p2p/rollouts/<id>. */
export interface RolloutDetail extends Rollout {
  rack_detail: RolloutRack[];
  log: string[];
  image_pin: { filename: string; sha256: string; bytes: number };
}

export interface RolloutsResponse {
  ok: boolean;
  rollouts: Rollout[];
  /** Racks the server's inventory places, for the picker. */
  racks: { rack: string; nodes: number }[];
  /** What is missing before a rollout can start on this cluster (empty = ready). */
  blockers: string[];
  defaults: RolloutPacing;
}

/** GET /api/p2p/rollouts/plan. */
export interface RolloutPlan {
  ok: boolean;
  image: { filename: string; sha256: string; bytes: number };
  racks: { rack: string; nodes: { node: string; agent: boolean; state: string }[]; live_agents: number; holders: number }[];
  metrics_error: string;
  pacing: RolloutPacing;
}

export const TERMINAL_ROLLOUT_STATES = new Set(["completed", "failed", "cancelled"]);
