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
import { apiFetch } from "../lib/apiFetch";

export interface SlurmNodeStatus {
  node: string;
  state: string;
  reason: string;
}

interface SlurmNodeStatusResponse {
  ok: boolean;
  nodes: SlurmNodeStatus[];
  source?: string;
  captured_at?: string;
  error?: string;
  stderr?: string;
}

export async function fetchSlurmNodeStatus(
  nodes: string[],
  signal?: AbortSignal,
): Promise<SlurmNodeStatusResponse> {
  const query = new URLSearchParams({ nodes: nodes.join(",") });
  const response = await apiFetch(`/api/scheduler-node-status?${query}`, {
    cache: "no-store",
    signal,
  });
  const data = (await response.json()) as SlurmNodeStatusResponse;
  if (!response.ok || !data.ok) {
    throw new Error(data.error || data.stderr || `Slurm status failed (HTTP ${response.status})`);
  }
  return data;
}
