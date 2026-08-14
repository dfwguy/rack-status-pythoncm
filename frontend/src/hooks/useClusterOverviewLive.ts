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
import type { ClusterOverviewLiveResponse } from "../types/api";
import { getClientId } from "./useRackStatus";

const POLL_MS = 30_000;
const INIT_POLL_MS = 3_000;

function anySectionInitializing(data: ClusterOverviewLiveResponse | null): boolean {
  if (!data?.sections) return false;
  return Object.values(data.sections).some((section) => section.state === "initializing");
}

function pollIntervalMs(data: ClusterOverviewLiveResponse | null): number {
  return anySectionInitializing(data) ? INIT_POLL_MS : POLL_MS;
}

export function useClusterOverviewLive(logDir: string) {
  const [data, setData] = useState<ClusterOverviewLiveResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dataRef = useRef<ClusterOverviewLiveResponse | null>(null);
  const requestSeqRef = useRef(0);
  const mountedRef = useRef(true);

  const fetchLive = useCallback(async (forceRefresh = false): Promise<number> => {
    const seq = ++requestSeqRef.current;
    try {
      const params = new URLSearchParams();
      if (logDir) params.set("log_dir", logDir);
      params.set("client_id", getClientId());
      if (forceRefresh) params.set("refresh", "1");
      const res = await fetch(`/api/cluster-overview/live?${params}`, { cache: "no-store" });
      const json = (await res.json()) as
        | ClusterOverviewLiveResponse
        | { ok: false; error?: string };

      if (!mountedRef.current || seq !== requestSeqRef.current) {
        return pollIntervalMs(dataRef.current);
      }

      if (!res.ok) {
        const msg =
          "error" in json && typeof json.error === "string" ? json.error : `HTTP ${res.status}`;
        throw new Error(msg);
      }
      if (!json.ok) {
        throw new Error(
          "error" in json
            ? String(json.error || "cluster overview live failed")
            : "cluster overview live failed",
        );
      }

      const fresh = json as ClusterOverviewLiveResponse;
      dataRef.current = fresh;
      setData(fresh);
      setError(null);
      return pollIntervalMs(fresh);
    } catch (err) {
      if (!mountedRef.current || seq !== requestSeqRef.current) {
        return pollIntervalMs(dataRef.current);
      }
      // Keep the last good live payload; surface the error non-destructively.
      setError(err instanceof Error ? err.message : "Unknown error");
      return pollIntervalMs(dataRef.current);
    } finally {
      if (mountedRef.current && seq === requestSeqRef.current) {
        setLoading(false);
      }
    }
  }, [logDir]);

  useEffect(() => {
    mountedRef.current = true;

    const poll = async () => {
      const intervalMs = await fetchLive();
      if (!mountedRef.current) return;
      timerRef.current = setTimeout(poll, intervalMs);
    };
    void poll();

    return () => {
      mountedRef.current = false;
      requestSeqRef.current += 1;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [fetchLive]);

  const retry = useCallback(() => fetchLive(true), [fetchLive]);

  return {
    data,
    loading,
    error,
    retry,
  };
}
