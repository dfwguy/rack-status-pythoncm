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
import type { DeepAuditRun, NodeIssuesResponse, PostRunNodeReportResponse } from "../types/api";
import { apiFetch } from "../lib/apiFetch";

async function jsonOrError<T>(res: Response): Promise<T> {
  const text = await res.text();
  let payload: Record<string, unknown>;
  try {
    payload = text ? (JSON.parse(text) as Record<string, unknown>) : {};
  } catch {
    throw new Error(`Invalid JSON (HTTP ${res.status}): ${text.slice(0, 240)}`);
  }
  if (!res.ok || payload.ok === false) {
    throw new Error(typeof payload.error === "string" ? payload.error : `HTTP ${res.status}`);
  }
  return payload as T;
}

export async function fetchNodeIssues(params: { rack?: string; node?: string; sync?: boolean } = {}): Promise<NodeIssuesResponse> {
  const query = new URLSearchParams();
  if (params.rack) query.set("rack", params.rack);
  if (params.node) query.set("node", params.node);
  if (params.sync) query.set("sync", "1");
  const res = await apiFetch(`/api/node-issues?${query}`, { credentials: "same-origin", cache: "no-store" });
  return jsonOrError<NodeIssuesResponse>(res);
}

export async function startDeepAudit(args: {
  workflowId: "hardware-deep-audit" | "nvlink-network-deep-audit";
  rack: string;
  target: string;
}): Promise<string> {
  const res = await apiFetch(`/api/workflows/${encodeURIComponent(args.workflowId)}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ params: { ...(args.rack ? { rack: args.rack } : {}), target: args.target } }),
  });
  const data = await jsonOrError<{ ok: boolean; run_id: string }>(res);
  return data.run_id;
}

export async function fetchDeepAudits(params: { rack: string; target?: string; kind: "hardware" | "network"; limit?: number }): Promise<DeepAuditRun[]> {
  const query = new URLSearchParams({ kind: params.kind });
  if (params.rack) query.set("rack", params.rack);
  if (params.target) query.set("target", params.target);
  if (params.limit) query.set("limit", String(params.limit));
  const res = await apiFetch(`/api/deep-audits?${query}`, { credentials: "same-origin", cache: "no-store" });
  const data = await jsonOrError<{ ok: boolean; runs: DeepAuditRun[] }>(res);
  return Array.isArray(data.runs) ? data.runs : [];
}

export async function fetchDeepAudit(runId: string): Promise<DeepAuditRun> {
  const res = await apiFetch(`/api/deep-audits/${encodeURIComponent(runId)}`, {
    credentials: "same-origin",
    cache: "no-store",
  });
  const data = await jsonOrError<{ ok: boolean; run: DeepAuditRun }>(res);
  return data.run;
}

export async function fetchDeepAuditOutput(runId: string, stepId = "audit"): Promise<string> {
  const res = await apiFetch(
    `/api/workflow-runs/${encodeURIComponent(runId)}/output/${encodeURIComponent(stepId)}`,
    { credentials: "same-origin", cache: "no-store" },
  );
  const data = await jsonOrError<{ ok: boolean; output: string }>(res);
  return typeof data.output === "string" ? data.output : "";
}

export async function fetchPostRunNodeReport(params: { rack?: string; node?: string; refresh?: boolean } = {}): Promise<PostRunNodeReportResponse> {
  const query = new URLSearchParams();
  if (params.rack) query.set("rack", params.rack);
  if (params.node) query.set("node", params.node);
  if (params.refresh) query.set("refresh", "1");
  const res = await apiFetch(`/api/post-run-node-report?${query}`, { credentials: "same-origin", cache: "no-store" });
  return jsonOrError<PostRunNodeReportResponse>(res);
}

export async function markPostRunNode(args: {
  node: string;
  category: string;
  reason: string;
  job_id?: string;
  validation_run_id?: string;
  evidence?: string;
  drain: boolean;
}): Promise<void> {
  const res = await apiFetch("/api/post-run-node-report/mark", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify(args),
  });
  await jsonOrError<{ ok: boolean }>(res);
}
