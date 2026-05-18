import { useCallback, useEffect, useRef, useState } from "react";
import type { RackStatusResponse } from "../types/api";
import { apiFetch, getClientId } from "../lib/apiFetch";

const DEFAULT_POLL_MS = 15_000;

/** Re-exported for legacy call sites; prefer importing from ../lib/apiFetch. */
export { getClientId };

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
      const res = await apiFetch(`/api/status?${params}`, { cache: "no-store" });
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
