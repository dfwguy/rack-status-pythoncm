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
import type { NodeStagesResponse } from "../types/api";
import { apiFetch } from "../lib/apiFetch";

/** Stage 1 / Stage 2 for each node of *rack* (``GET /api/node-stages?rack=``). */
export async function fetchNodeStages(params: { rack?: string; node?: string; logDir?: string }): Promise<NodeStagesResponse> {
  const query = new URLSearchParams();
  if (params.rack) query.set("rack", params.rack);
  if (params.node) query.set("node", params.node);
  if (params.logDir) query.set("log_dir", params.logDir);
  const res = await apiFetch(`/api/node-stages?${query}`, { credentials: "same-origin", cache: "no-store" });
  const text = await res.text();
  let payload: NodeStagesResponse;
  try {
    payload = (text ? JSON.parse(text) : {}) as NodeStagesResponse;
  } catch {
    throw new Error(`Invalid JSON (HTTP ${res.status}): ${text.slice(0, 240)}`);
  }
  if (!res.ok || payload.ok === false) {
    throw new Error(payload.error || `HTTP ${res.status}`);
  }
  return payload;
}
