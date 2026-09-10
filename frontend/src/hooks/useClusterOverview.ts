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
import { useCallback, useEffect, useRef, useState } from "react";
import type { ClusterOverviewResponse } from "../types/api";

const emptyClusterDashboardMetadata = (): ClusterOverviewResponse["cluster_dashboard_metadata"] => ({
  product_line: "",
  hw_vendor: "",
  customer_name: "",
});
import { getClientId } from "./useRackStatus";
import { apiFetch } from "../lib/apiFetch";

const POLL_MS = 30_000;

export function useClusterOverview(logDir: string) {
  const [data, setData] = useState<ClusterOverviewResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchOverview = useCallback(async (forceRefresh = false) => {
    setRefreshing(true);
    try {
      const params = new URLSearchParams();
      if (logDir) params.set("log_dir", logDir);
      params.set("client_id", getClientId());
      if (forceRefresh) params.set("refresh", "1");
      const res = await apiFetch(`/api/cluster-overview?${params}`, { cache: "no-store" });
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

      setData(fresh);
      setError(null);
      if (fresh.snapshot.id && (fresh.snapshot.state === "fresh" || fresh.snapshot.state === "stale")) {
        setLastUpdated(Date.now());
      }
      return fresh.snapshot.state;
    } catch (err) {
      // Keep the last good data on screen; surface the error non-destructively.
      setError(err instanceof Error ? err.message : "Unknown error");
      return undefined;
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [logDir]);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      const state = await fetchOverview();
      if (cancelled) return;
      timerRef.current = setTimeout(poll, state === "initializing" ? 3_000 : POLL_MS);
    };
    void poll();
    return () => {
      cancelled = true;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [fetchOverview]);

  return {
    data,
    loading,
    refreshing,
    error,
    lastUpdated,
    refresh: () => fetchOverview(false),
    retry: () => fetchOverview(true),
  };
}
