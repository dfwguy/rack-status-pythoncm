import { useCallback, useEffect, useState } from "react";
import { getBringupNode } from "../services/bringupService";
import type { BringupNodeSummary } from "../types/bringup";

interface UseBringupNode {
  data: BringupNodeSummary | null;
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

/**
 * Fetch a node's bring-up summary on selection and on demand. Deliberately no
 * polling: each call shells out to Travis's cluster-wide report, so refreshing
 * is operator-driven rather than a timer hammering the head node.
 */
export function useBringupNode(node: string | null): UseBringupNode {
  const [data, setData] = useState<BringupNodeSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (target: string, signal?: AbortSignal) => {
      setLoading(true);
      setError(null);
      try {
        const summary = await getBringupNode(target);
        if (!signal?.aborted) setData(summary);
      } catch (err) {
        if (!signal?.aborted) {
          setError(err instanceof Error ? err.message : "Failed to load bring-up results");
          setData(null);
        }
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (!node) return;
    const ctrl = new AbortController();
    // Fetch-on-change: load() flips loading/error as it synchronizes with the
    // backend. This is the sanctioned "subscribe to an external system" effect,
    // and the state is reset by load() itself, not duplicated here.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load(node, ctrl.signal);
    return () => ctrl.abort();
  }, [node, load]);

  const refresh = useCallback(() => {
    if (node) void load(node);
  }, [node, load]);

  // Derive the no-node output rather than resetting state in the effect (which
  // would be a cascading setState). The panel only mounts with a real node, so
  // this is a defensive guard.
  return node
    ? { data, loading, error, refresh }
    : { data: null, loading: false, error: null, refresh };
}
