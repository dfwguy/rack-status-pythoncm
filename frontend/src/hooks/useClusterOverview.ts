import { useCallback, useEffect, useRef, useState } from "react";
import type { ClusterOverviewResponse } from "../types/api";

const emptyClusterDashboardMetadata = (): ClusterOverviewResponse["cluster_dashboard_metadata"] => ({
  product_line: "",
  hw_vendor: "",
  customer_name: "",
});
import { getClientId } from "./useRackStatus";

const POLL_MS = 30_000;
const CACHE_KEY = "nvis.clusterOverview.lastGood";

function loadCached(): ClusterOverviewResponse | null {
  try {
    if (typeof localStorage === "undefined") return null;
    const raw = localStorage.getItem(CACHE_KEY);
    return raw ? (JSON.parse(raw) as ClusterOverviewResponse) : null;
  } catch {
    return null;
  }
}

function saveCached(d: ClusterOverviewResponse): void {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify(d));
  } catch {
    /* ignore quota / serialization errors */
  }
}

/**
 * A payload is "ready" once the rack-status cache it derives from is warm. While
 * the cache is cold the backend reports zeroed rack/tray/validation counts; we use
 * those as a signal to keep the previous good stats rather than flashing zeros.
 */
function statsReady(d: ClusterOverviewResponse): boolean {
  return (d.rack_count ?? 0) > 0 || (d.tray_hosts ?? 0) > 0 || Boolean(d.status_updated_at);
}

/** Carry forward the last-good rack-derived stats when a fresh payload is still cold. */
function mergePreservingStats(
  fresh: ClusterOverviewResponse,
  prev: ClusterOverviewResponse
): ClusterOverviewResponse {
  return {
    ...fresh,
    rack_count: prev.rack_count,
    tray_hosts: prev.tray_hosts,
    estimated_gpu_count: prev.estimated_gpu_count,
    validation_summary: prev.validation_summary,
    validation_cells_total: prev.validation_cells_total,
    status_updated_at: prev.status_updated_at,
  };
}

export function useClusterOverview(logDir: string) {
  const [data, setData] = useState<ClusterOverviewResponse | null>(loadCached);
  // Only block the UI with a skeleton when there is nothing cached to show.
  const [loading, setLoading] = useState(() => loadCached() === null);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dataRef = useRef<ClusterOverviewResponse | null>(data);
  dataRef.current = data;

  const fetchOverview = useCallback(async () => {
    setRefreshing(true);
    try {
      const params = new URLSearchParams();
      if (logDir) params.set("log_dir", logDir);
      params.set("client_id", getClientId());
      const res = await fetch(`/api/cluster-overview?${params}`, { cache: "no-store" });
      const json = (await res.json()) as ClusterOverviewResponse | { ok: false; error?: string };
      if (!res.ok) {
        const msg =
          "error" in json && typeof json.error === "string" ? json.error : `HTTP ${res.status}`;
        throw new Error(msg);
      }
      if (!json.ok) {
        throw new Error("error" in json ? String(json.error || "cluster overview failed") : "cluster overview failed");
      }
      const fresh: ClusterOverviewResponse = {
        ...(json as ClusterOverviewResponse),
        cluster_dashboard_metadata:
          (json as ClusterOverviewResponse).cluster_dashboard_metadata ?? emptyClusterDashboardMetadata(),
      };

      const prev = dataRef.current;
      const ready = statsReady(fresh);
      // Cold payload + we already have good stats -> keep the stats, refresh the rest.
      const next = !ready && prev && statsReady(prev) ? mergePreservingStats(fresh, prev) : fresh;

      setData(next);
      setError(null);
      if (statsReady(next)) {
        saveCached(next);
        setLastUpdated(Date.now());
      }
    } catch (err) {
      // Keep the last good data on screen; surface the error non-destructively.
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [logDir]);

  useEffect(() => {
    fetchOverview();
    const poll = () => {
      timerRef.current = setTimeout(async () => {
        await fetchOverview();
        poll();
      }, POLL_MS);
    };
    poll();
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [fetchOverview]);

  return { data, loading, refreshing, error, lastUpdated, refresh: fetchOverview };
}
