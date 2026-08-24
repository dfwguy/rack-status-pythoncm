/* SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 */
// launchReview.ts — bind a reviewed launch target to the launch that follows it (REQ-6).
// Author: Rafael M. Koike & Cursor Agent ("Ada")

import { apiFetch } from "./apiFetch";

/**
 * Legacy launch URL -> the `kind` that `POST /api/launch/prepare` understands.
 *
 * The backend requires a `plan_hash` on these endpoints: it resolves the target, hands back a
 * digest, and refuses the launch if a fresh resolution disagrees. That closes the case where the
 * operator reviewed one target and a different one ran — a drained rack, a superseded campaign, or
 * simply a tab left open while someone switched clusters.
 *
 * A map rather than a `kind` threaded through every call site: the callers already pass a URL, and
 * adding a parallel argument to each would be one more thing to get wrong per launch.
 */
export const LAUNCH_KIND_BY_URL: Readonly<Record<string, string>> = {
  "/api/run-validation": "validation",
  "/api/run-validation-async": "validation",
  "/api/run-nemo-xrack-async": "nemo-xrack",
  "/api/run-exemplar-llama-async": "exemplar-llama",
  "/api/run-llama-async": "llama",
  "/api/run-mnubergemm-stress-async": "mnubergemm-stress",
  "/api/run-nccl-fullrack-async": "nccl-fullrack",
  "/api/run-nvloom-async": "nvloom",
  "/api/run-clusterkit-async": "clusterkit",
  "/api/run-llmb-nccl-async": "llmb-nccl",
};

export interface PreparedLaunch {
  target: string;
  rack: string;
  command_display: string;
  log_dir: string;
  timeout_sec?: number;
}

/** What `prepare` resolved, plus the digest that binds it. */
export interface LaunchReview {
  plan: PreparedLaunch;
  planHash: string;
}

/**
 * Resolve a launch for review without starting it.
 *
 * Returns `null` for a URL with no registered kind, which lets a caller fall through to a plain
 * POST — the preset path has its own stronger binding and must not be double-gated, and an endpoint
 * genuinely outside this scheme should not be silently blocked by it.
 */
export async function prepareLaunch(
  postUrl: string,
  body: Record<string, unknown>,
): Promise<LaunchReview | null> {
  const kind = LAUNCH_KIND_BY_URL[postUrl];
  if (!kind) return null;

  const res = await apiFetch("/api/launch/prepare", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, ...body }),
  });
  const data = (await res.json()) as {
    ok?: boolean;
    plan?: PreparedLaunch;
    plan_hash?: string;
    error?: string;
  };
  if (!res.ok || !data.ok || !data.plan_hash) {
    // Surfaced rather than swallowed: a prepare that fails is the launch failing early, and its
    // message is the specific one (bad rack, disabled tool, unresolvable target) that the operator
    // needs. Falling through to the launch would replace it with a confusing digest complaint.
    throw new Error(data.error || "could not resolve the launch target for review");
  }
  return { plan: data.plan ?? ({} as PreparedLaunch), planHash: data.plan_hash };
}

/**
 * POST a launch with its reviewed-target digest attached.
 *
 * One round trip more than a bare POST, and it buys the guarantee that the target which was
 * resolved is the target that runs. For a URL outside `LAUNCH_KIND_BY_URL` this is exactly a plain
 * POST, so it is safe to route every launch through it.
 */
export async function launchWithReview(
  postUrl: string,
  body: Record<string, unknown>,
): Promise<Response> {
  const review = await prepareLaunch(postUrl, body);
  const payload = review ? { ...body, plan_hash: review.planHash } : body;
  return apiFetch(postUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}
