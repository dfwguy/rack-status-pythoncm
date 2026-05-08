import { useCallback, useEffect, useRef, useState } from "react";
import type { ClusterOverviewResponse } from "../types/api";

const emptyClusterDashboardMetadata = (): ClusterOverviewResponse["cluster_dashboard_metadata"] => ({
  product_line: "",
  hw_vendor: "",
  customer_name: "",
});
import { getClientId } from "./useRackStatus";

const POLL_MS = 30_000;

export function useClusterOverview(logDir: string) {
  const [data, setData] = useState<ClusterOverviewResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchOverview = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (logDir) params.set("log_dir", logDir);
      params.set("client_id", getClientId());
      const res = await fetch(`/api/cluster-overview?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = (await res.json()) as ClusterOverviewResponse | { ok: false; error?: string };
      if (!json.ok) throw new Error("error" in json ? String(json.error || "cluster overview failed") : "cluster overview failed");
      const j = json as ClusterOverviewResponse;
      setData({
        ...j,
        cluster_dashboard_metadata: j.cluster_dashboard_metadata ?? emptyClusterDashboardMetadata(),
      });
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }, [logDir]);

  useEffect(() => {
    setLoading(true);
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

  return { data, loading, error, refresh: fetchOverview };
}
