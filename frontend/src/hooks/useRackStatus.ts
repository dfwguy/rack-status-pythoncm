import { useCallback, useEffect, useRef, useState } from "react";
import type { RackStatusResponse } from "../types/api";

const DEFAULT_POLL_MS = 15_000;
const CLIENT_ID_KEY = "nvis-rack-client-id";

let _clientId: string | null = null;
function getClientId(): string {
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
  const [activeUsers, setActiveUsers] = useState(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchStatus = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (logDir) params.set("log_dir", logDir);
      params.set("client_id", getClientId());
      const res = await fetch(`/api/status?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json: RackStatusResponse & { active_clients?: number } = await res.json();
      setData(json);
      setError(null);
      if (json.active_clients != null) {
        setActiveUsers(json.active_clients);
      }
    } catch (err) {
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

  return { data, loading, error, refresh: fetchStatus, activeUsers };
}
