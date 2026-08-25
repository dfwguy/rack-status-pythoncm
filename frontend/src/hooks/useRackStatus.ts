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
import type { RackStatusResponse } from "../types/api";

// Keep the Racks view aligned with the server's 30-second PythonCM snapshot cadence.
export const DEFAULT_RACKS_POLL_MS = 30_000;
const CLIENT_ID_KEY = "nvis-rack-client-id";

let _clientId: string | null = null;
/** Shared client id for /api/status and /api/cluster-overview active-user counting. */
export function getClientId(): string {
  if (!_clientId) {
    _clientId = localStorage.getItem(CLIENT_ID_KEY);
    if (!_clientId) {
      _clientId = `spa-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
      localStorage.setItem(CLIENT_ID_KEY, _clientId);
    }
  }
  return _clientId;
}

export function useRackStatus(logDir: string, pollMs = DEFAULT_RACKS_POLL_MS) {
  const [data, setData] = useState<RackStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reachable, setReachable] = useState(true);
  const [unreachableDetail, setUnreachableDetail] = useState<string | null>(null);
  /** True when the last poll reached the Flask backend (UI ↔ backend link). */
  const [backendConnected, setBackendConnected] = useState(false);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [activeUsers, setActiveUsers] = useState(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dataRef = useRef<RackStatusResponse | null>(null);

  const fetchStatus = useCallback(async (forceRefresh = false) => {
    try {
      const params = new URLSearchParams();
      if (logDir) params.set("log_dir", logDir);
      params.set("client_id", getClientId());
      if (forceRefresh) params.set("refresh", "1");
      const res = await fetch(`/api/status?${params}`, { cache: "no-store" });
      const json: RackStatusResponse & { active_clients?: number; error?: string } = await res.json();
      // We got a response from the Flask backend (even a degraded one).
      setBackendConnected(true);
      if (!res.ok) {
        throw new Error(json.error || `HTTP ${res.status}`);
      }
      const isUnreachable = json.cluster_reachable === false || json.degraded === true;
      if (!isUnreachable) setLastUpdated(Date.now());
      setReachable(!isUnreachable);
      setUnreachableDetail(isUnreachable ? json.cluster_unreachable_detail || "Cluster unreachable" : null);
      // Keep the last good table on screen while the link is down instead of
      // replacing it with an unavailable placeholder. A stale snapshot already
      // contains canonical last-good rows and must replace the prior fresh state.
      const hasSnapshotPayload =
        Boolean(json.snapshot?.id) &&
        (json.snapshot?.state === "fresh" || json.snapshot?.state === "stale");
      if (isUnreachable && dataRef.current && !hasSnapshotPayload) {
        // preserve previous data; only refresh comments if the placeholder carries them
        if (json.comments) {
          const preserved = { ...dataRef.current, comments: json.comments };
          dataRef.current = preserved;
          setData(preserved);
        }
      } else {
        dataRef.current = json;
        setData(json);
      }
      setError(null);
      if (json.active_clients != null) {
        setActiveUsers(json.active_clients);
      }
      return json.snapshot?.state;
    } catch (err) {
      // A TypeError from fetch means we never reached the backend (server down /
      // page offline); an HTTP error means the backend answered but failed.
      if (err instanceof TypeError) setBackendConnected(false);
      setError(err instanceof Error ? err.message : "Unknown error");
      return undefined;
    } finally {
      setLoading(false);
    }
  }, [logDir]);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      const state = await fetchStatus();
      if (cancelled) return;
      timerRef.current = setTimeout(poll, state === "initializing" ? 3_000 : pollMs);
    };
    void poll();

    return () => {
      cancelled = true;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [fetchStatus, pollMs]);

  return {
    data,
    loading,
    error,
    reachable,
    unreachableDetail,
    backendConnected,
    lastUpdated,
    refresh: () => fetchStatus(false),
    retry: () => fetchStatus(true),
    activeUsers,
  };
}
