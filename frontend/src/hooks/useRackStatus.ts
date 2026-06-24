import { useCallback, useEffect, useRef, useState } from "react";
import type { RackStatusResponse } from "../types/api";

const DEFAULT_POLL_MS = 15_000;
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

export function useRackStatus(logDir: string, pollMs = DEFAULT_POLL_MS) {
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
  dataRef.current = data;

  const fetchStatus = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (logDir) params.set("log_dir", logDir);
      params.set("client_id", getClientId());
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
      // replacing it with the empty degraded placeholder.
      if (isUnreachable && dataRef.current) {
        // preserve previous data; only refresh comments if the placeholder carries them
        if (json.comments) {
          setData({ ...dataRef.current, comments: json.comments });
        }
      } else {
        setData(json);
      }
      setError(null);
      if (json.active_clients != null) {
        setActiveUsers(json.active_clients);
      }
    } catch (err) {
      // A TypeError from fetch means we never reached the backend (server down /
      // page offline); an HTTP error means the backend answered but failed.
      if (err instanceof TypeError) setBackendConnected(false);
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }, [logDir]);

  useEffect(() => {
    setLoading(true);
    fetchStatus();

    const poll = () => {
      timerRef.current = setTimeout(async () => {
        await fetchStatus();
        poll();
      }, pollMs);
    };
    poll();

    return () => {
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
    refresh: fetchStatus,
    activeUsers,
  };
}
