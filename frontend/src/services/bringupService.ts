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
import type { BringupConfig, BringupFleet, BringupNodeSummary } from "../types/bringup";

/**
 * Parse a JSON API response, or explain why it is not JSON.
 *
 * A bare `res.json()` here surfaced `Unexpected token '<', "<html> <"...`, which tells an
 * operator nothing. HTML from an `/api/*` path has two ordinary causes worth naming: the SPA
 * catch-all served `index.html` because the route does not exist in the running backend (it
 * was added but the process has not restarted — bind mounts change the file, not the loaded
 * routes), or an auth layer redirected to a login page.
 */
/** Path without the query, for error messages. */
function forMessage(url: string): string {
  return url.split("?")[0];
}

/**
 * POST a JSON payload, or GET when there is nothing to send.
 *
 * Cluster-sized arguments belong in a body, not a URL: a 591-node list is a ~12 KB query string
 * and gunicorn rejects any request line over 4094 bytes with a bare 400. Request bodies have no
 * such limit, so the node list travels as JSON.
 */
function jsonPost(payload: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  };
}

async function apiJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(url, init);
  const body = await res.text();
  let data: { ok?: boolean; error?: string };
  try {
    data = JSON.parse(body) as { ok?: boolean; error?: string };
  } catch {
    const looksHtml = /^\s*<(?:!doctype|html)/i.test(body);
    throw new Error(
      looksHtml
        ? `${forMessage(url)} returned an HTML page instead of JSON (HTTP ${res.status}). ` +
          "Either the session expired — reload and sign in — or the backend does not have " +
          "this endpoint yet and needs a restart."
        : `${forMessage(url)} returned a non-JSON response (HTTP ${res.status}): ` +
          body.slice(0, 120),
    );
  }
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data as T;
}

/** Discovery: whether the bring-up tree is present and enabled. */
export async function getBringupConfig(): Promise<BringupConfig> {
  const data = await apiJson<{ ok: true } & BringupConfig>("/api/bringup/config");
  return { enabled: data.enabled, present: data.present, home: data.home };
}

/** Authoritative per-node bring-up job summary. */
export async function getBringupNode(node: string): Promise<BringupNodeSummary> {
  const data = await apiJson<{ ok: true; data: BringupNodeSummary }>(
    `/api/bringup/node?node=${encodeURIComponent(node)}`,
  );
  return data.data;
}

/**
 * Every node's bring-up state in one request.
 *
 * `nodes` is only read in demo mode, where the backend has no cluster to enumerate and seeds
 * the synthetic fleet from the names the dashboard is already showing.
 */
export async function getBringupFleet(nodes?: string[]): Promise<BringupFleet> {
  const data = await apiJson<{ ok: true; data: BringupFleet }>(
    "/api/bringup/fleet",
    jsonPost({ nodes: nodes ?? [] }),
  );
  return data.data;
}

/**
 * Download the daily node report.
 *
 * The document is built and schema-validated by the backend, so this deliberately does no
 * shaping — it saves exactly what was validated. Errors are surfaced rather than saving a
 * response body that is actually a JSON error payload.
 */
/** Live position of a running report job: one tick per node swept. */
export interface NodeReportProgress {
  state?: "running" | "done" | "collected" | "error";
  phase?: string;
  done?: number;
  total?: number;
  current?: string;
  error?: string;
  ready?: boolean;
  report_id?: string;
  nodes?: number;
}

/**
 * Generate the node report as a background job, reporting progress as it goes.
 *
 * Building it sweeps every node over SSH — minutes on a large fleet — so a blocking request
 * would sit behind a spinner long enough for a proxy to give up. `onProgress` is called with
 * the server's own count so the caller can show how far through the fleet the sweep is.
 *
 * Starting the same run twice returns the same job: the backend joins the press to the run
 * already in flight rather than sweeping the cluster a second time.
 */
export async function generateNodeReport(
  onProgress?: (p: NodeReportProgress) => void,
  pollMs = 1000,
  options?: { report?: string; format?: string },
): Promise<string> {
  const started = await apiFetch(
    "/api/node-report/start",
    jsonPost({ inventory: true, ...(options ?? {}) }),
  );
  const startBody = (await started.json()) as { ok?: boolean; job_id?: string; error?: string };
  if (!started.ok || !startBody.job_id) {
    throw new Error(startBody?.error || `HTTP ${started.status}`);
  }
  const jobId = startBody.job_id;

  for (;;) {
    await new Promise((r) => setTimeout(r, pollMs));
    const res = await apiFetch(`/api/node-report/status?job_id=${encodeURIComponent(jobId)}`);
    const status = (await res.json()) as NodeReportProgress & { ok?: boolean };
    if (!res.ok) throw new Error(status?.error || `HTTP ${res.status}`);
    onProgress?.(status);
    if (status.state === "error") throw new Error(status.error || "report generation failed");
    if (status.ready) break;
  }
  return downloadNodeReportResult(jobId);
}

async function downloadNodeReportResult(jobId: string): Promise<string> {
  const res = await apiFetch(
    `/api/node-report/result?job_id=${encodeURIComponent(jobId)}&download=1`,
  );
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      detail = ((await res.json()) as { error?: string })?.error || detail;
    } catch {
      // Non-JSON error body (a proxy page, say); the status line is the best we have.
    }
    throw new Error(detail);
  }
  return saveJsonResponse(res);
}

export async function downloadNodeReport(nodes?: string[]): Promise<string> {
  const res = await apiFetch("/api/node-report", jsonPost({ nodes: nodes ?? [], download: true }));
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { error?: string };
      detail = body?.error || detail;
    } catch {
      // Non-JSON error body (a proxy page, say); the status line is the best we have.
    }
    throw new Error(detail);
  }
  return saveJsonResponse(res);
}

/** Save a validated JSON response to disk under the filename the server chose. */
async function saveJsonResponse(res: Response): Promise<string> {
  const blob = await res.blob();
  const filename =
    /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] ||
    "node-report.json";
  const url = URL.createObjectURL(blob);
  try {
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
  return filename;
}
