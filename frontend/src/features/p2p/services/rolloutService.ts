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
// Pull rollouts: the /api/p2p/rollouts calls. Every control (cancel, retry, pace) acts on a live
// workflow-engine run; the server applies it on the rollout's next tick.
import { apiJson } from "../lib/apiFetch";
import type { Rollout, RolloutDetail, RolloutPacing, RolloutPlan, RolloutsResponse } from "../types/rollout";

const BASE = "/api/p2p/rollouts";
const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export function listRollouts(): Promise<RolloutsResponse> {
  return apiJson<RolloutsResponse>(BASE);
}

export async function getRollout(runId: string): Promise<RolloutDetail> {
  const res = await apiJson<{ ok: boolean; rollout: RolloutDetail }>(`${BASE}/${encodeURIComponent(runId)}`);
  return res.rollout;
}

export function planRollout(image: string, racks: string[]): Promise<RolloutPlan> {
  const q = new URLSearchParams({ image, racks: racks.join(",") });
  return apiJson<RolloutPlan>(`${BASE}/plan?${q.toString()}`);
}

export function startRollout(
  image: string,
  racks: string[],
  pacing: Partial<RolloutPacing>,
): Promise<{ ok: boolean; run_id: string; joined?: boolean }> {
  return apiJson(BASE, json("POST", { image, racks, pacing }));
}

export function cancelRollout(runId: string): Promise<{ ok: boolean; state: string }> {
  return apiJson(`${BASE}/${encodeURIComponent(runId)}/cancel`, json("POST", {}));
}

/** Retry *nodes*; an empty list retries every deferred node. */
export async function retryRollout(runId: string, nodes: string[]): Promise<Rollout> {
  const res = await apiJson<{ ok: boolean; rollout: Rollout }>(
    `${BASE}/${encodeURIComponent(runId)}/retry`,
    json("POST", { nodes }),
  );
  return res.rollout;
}

export async function setRolloutPacing(runId: string, pacing: Partial<RolloutPacing>): Promise<Rollout> {
  const res = await apiJson<{ ok: boolean; rollout: Rollout }>(
    `${BASE}/${encodeURIComponent(runId)}/pacing`,
    json("PUT", pacing),
  );
  return res.rollout;
}
