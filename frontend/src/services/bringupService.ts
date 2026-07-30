import { apiFetch } from "../lib/apiFetch";
import type { BringupConfig, BringupFleet, BringupNodeSummary } from "../types/bringup";

async function apiJson<T>(url: string): Promise<T> {
  const res = await apiFetch(url);
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    throw new Error(data?.error || `HTTP ${res.status}`);
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
  const query = nodes?.length ? `?nodes=${encodeURIComponent(nodes.join(","))}` : "";
  const data = await apiJson<{ ok: true; data: BringupFleet }>(`/api/bringup/fleet${query}`);
  return data.data;
}

/**
 * Download the daily node report.
 *
 * The document is built and schema-validated by the backend, so this deliberately does no
 * shaping — it saves exactly what was validated. Errors are surfaced rather than saving a
 * response body that is actually a JSON error payload.
 */
export async function downloadNodeReport(nodes?: string[]): Promise<string> {
  const params = new URLSearchParams({ download: "1" });
  if (nodes?.length) params.set("nodes", nodes.join(","));
  const res = await apiFetch(`/api/node-report?${params.toString()}`);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      detail = body?.error || detail;
    } catch {
      // Non-JSON error body; the status line is the best we have.
    }
    throw new Error(detail);
  }
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
