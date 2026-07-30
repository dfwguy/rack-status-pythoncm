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
