import { apiFetch } from "../lib/apiFetch";

export interface WorkflowStep {
  id: string;
  type: "automated" | "manual" | "decision" | "ai_query" | "delay";
  name: string;
  command?: string;
  method?: string;
  body?: Record<string, unknown>;
  instructions?: string;
  question?: string;
  options?: { label: string; next: string; id?: string }[];
  on_success?: string;
  on_failure?: string;
  on_complete?: string;
  timeout?: number;
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

export interface WorkflowRun {
  run_id: string;
  workflow_id: string;
  workflow_name: string;
  state: string;
  current_step_id: string | null;
  params?: Record<string, string>;
  history?: Array<{ step_id: string; action?: unknown; completed_at: string }>;
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
