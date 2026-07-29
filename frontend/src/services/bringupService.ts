import { apiFetch } from "../lib/apiFetch";
import type { BringupConfig, BringupNodeSummary } from "../types/bringup";

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
