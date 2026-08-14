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

/** Per-step composition axes (all optional; absent == today's behavior). */
export interface StepConnection {
  profile?: "control" | "script";
  exec?: "local" | "ssh";
  sudo?: boolean;
}

export interface StepScheduler {
  kind?: "slurm" | "local" | "runai" | "slinky" | "maas";
  nodes?: number | string;
  nodelist?: string;
  ntasks_per_node?: number | string;
  script?: string;
  command?: string;
  image?: string;
  output?: string;
  reservation?: string;
}

export interface StepEvaluation {
  success_when?: Record<string, unknown>;
  extract?: { name: string; regex: string; type?: string; source?: string }[];
  assert?: string[];
}

/** One hop in an N-level connection chain (app -> [vpn] -> head -> ... -> node). */
export interface HopSpec {
  kind?: "local" | "ssh" | "vpn";
  host?: string;
  user?: string;
  key?: string;
  port?: string;
  sudo?: boolean;
  extra_opts?: string[];
  connect_timeout?: string;
}

/** Resolve rack(s) -> a real node list, stored as a run output (default "nodelist"). */
export interface StepResolve {
  racks?: string;
  into?: string;
}

/** Fan-out a step's `run` across N nodes over a composed Connection chain. */
export interface StepFanout {
  nodes?: string;          // targets for the final (fanned) hop; e.g. "${nodelist}"
  hops?: HopSpec[];        // shared prefix (app->...->gateway); omitted = app control connection
  node?: HopSpec;          // per-node hop template (host filled from nodes)
  max_workers?: number;
}

export interface WorkflowStep {
  id: string;
  type: "automated" | "manual" | "decision" | "ai_query" | "delay";
  name: string;
  command?: string;
  capability?: string;
  run?: string;
  method?: string;
  body?: Record<string, unknown>;
  parameters?: Record<string, string>;
  instructions?: string;
  question?: string;
  options?: { label: string; next: string; id?: string }[];
  description?: string;
  output?: string;
  timeout?: number;
  retries?: number;
  on_success?: string;
  on_failure?: string;
  on_complete?: string;
  // Composition axes:
  connection?: StepConnection;
  scheduler?: StepScheduler;
  evaluation?: StepEvaluation;
  fanout?: StepFanout;
  resolve?: StepResolve;
}

export interface WorkflowTarget {
  mode: "rack" | "nodes";
  racks: string[];
  nodes: string[];
}

export interface Workflow {
  id: string;
  name: string;
  description?: string;
  version?: number;
  mermaid?: string;
  steps: WorkflowStep[];
  target?: WorkflowTarget;
}

export interface WorkflowSummary {
  id: string;
  name: string;
  description: string;
  version: number;
  step_count: number;
}

/**
 * One completed step in a run's history.
 *
 * Every field past `step_id` is optional because the engine writes different keys per step type
 * (an automated step reports success plus its output, a manual step reports a prompt and a
 * message). The panel previously typed these entries `any`, which is what let the extra fields
 * go undeclared in the first place.
 */
export interface WorkflowRunHistoryEntry {
  step_id: string;
  action?: unknown;
  completed_at: string;
  /** Step type as the engine recorded it (automated / manual / decision). */
  type?: string;
  success?: boolean;
  message?: string;
  prompt?: string;
  /** Head of the step's captured output, for the panel; the full text is fetched on demand. */
  output_snippet?: string;
  output_file?: string;
}

export interface WorkflowRun {
  run_id: string;
  workflow_id: string;
  workflow_name: string;
  state: string;
  current_step_id: string | null;
  params?: Record<string, string>;
  history?: WorkflowRunHistoryEntry[];
  error?: string;
  started_at: string;
  updated_at: string;
}

export interface WorkflowRunSummary {
  run_id: string;
  workflow_id: string;
  workflow_name: string;
  state: string;
  current_step_id: string | null;
  started_at: string;
  updated_at: string;
}

async function apiJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(url, init);
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data as T;
}

export async function listWorkflows(): Promise<WorkflowSummary[]> {
  const data = await apiJson<{ workflows: WorkflowSummary[] }>("/api/workflows");
  return data.workflows;
}

export async function getWorkflow(id: string): Promise<Workflow> {
  const data = await apiJson<{ workflow: Workflow }>(`/api/workflows/${id}`);
  return data.workflow;
}

export async function saveWorkflow(workflow: Partial<Workflow> & { name: string }): Promise<Workflow> {
  const data = await apiJson<{ workflow: Workflow }>("/api/workflows", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(workflow),
  });
  return data.workflow;
}

export async function deleteWorkflow(id: string): Promise<void> {
  await apiJson(`/api/workflows/${id}`, { method: "DELETE" });
}

export async function startWorkflowRun(workflowId: string, params?: Record<string, string>): Promise<{ run_id: string; state: string }> {
  return apiJson(`/api/workflows/${workflowId}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ params }),
  });
}

export async function listRuns(): Promise<WorkflowRunSummary[]> {
  const data = await apiJson<{ runs: WorkflowRunSummary[] }>("/api/workflow-runs");
  return data.runs;
}

export async function getRunState(runId: string): Promise<WorkflowRun> {
  const data = await apiJson<{ run: WorkflowRun }>(`/api/workflow-runs/${runId}`);
  return data.run;
}

export async function advanceRun(runId: string, action: Record<string, unknown>): Promise<{ state: string }> {
  return apiJson(`/api/workflow-runs/${runId}/advance`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(action),
  });
}

export async function cancelRun(runId: string): Promise<{ state: string }> {
  return apiJson(`/api/workflow-runs/${runId}/cancel`, { method: "POST" });
}

export async function generateMermaid(steps: WorkflowStep[]): Promise<string> {
  const data = await apiJson<{ mermaid: string }>("/api/workflows/generate-mermaid", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ steps }),
  });
  return data.mermaid;
}
