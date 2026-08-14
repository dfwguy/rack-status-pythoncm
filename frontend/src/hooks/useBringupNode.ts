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
import { useCallback, useEffect, useReducer, useState } from "react";
import { getBringupNode } from "../services/bringupService";
import type { BringupNodeSummary } from "../types/bringup";

interface CacheEntry {
  data: BringupNodeSummary;
  fetchedAt: number;
}

/**
 * Module-level cache, keyed by node, shared by every mount (same pattern as
 * `lib/useGrafanaDeeplinks.ts`). It exists because `/api/bringup/node` shells out
 * to the cluster-wide `daily-report.sh`, which takes tens of seconds — so
 * re-selecting a node must render from cache instantly rather than re-run it.
 */
const _cache = new Map<string, CacheEntry>();
/** In-flight requests, so two mounts asking for the same node share one call. */
const _inflight = new Map<string, Promise<BringupNodeSummary>>();

/** Drop cached summaries (used by tests, and after actions that change results). */
export function clearBringupCache(node?: string): void {
  if (node) {
    _cache.delete(node);
    _inflight.delete(node);
  } else {
    _cache.clear();
    _inflight.clear();
  }
}

function fetchSummary(node: string): Promise<BringupNodeSummary> {
  const existing = _inflight.get(node);
  if (existing) return existing;
  const p = getBringupNode(node)
    .then((data) => {
      _cache.set(node, { data, fetchedAt: Date.now() });
      return data;
    })
    .finally(() => {
      _inflight.delete(node);
    });
  _inflight.set(node, p);
  return p;
}

interface UseBringupNode {
  data: BringupNodeSummary | null;
  loading: boolean;
  error: string | null;
  /** When the shown data was fetched (epoch ms), or null when nothing is cached. */
  lastUpdated: number | null;
  refresh: () => void;
}

/**
 * Per-node bring-up summary with a shared cache and optional auto-refresh.
 *
 * Lazy: a cached node renders immediately with no spinner, and is only re-fetched
 * when the cached copy is older than `refreshMs` (or on an explicit `refresh()`).
 * Auto-refresh ticks are skipped while the tab is hidden, since each one costs a
 * cluster-wide report on the head node.
 *
 * @param refreshMs 0 disables auto-refresh; otherwise the cadence in ms.
 */
export function useBringupNode(node: string | null, refreshMs = 0): UseBringupNode {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The cache lives outside React, so a write cannot trigger a render on its own;
  // bump() is purely that re-render signal (its counter value is never read).
  const [, bump] = useReducer((n: number) => n + 1, 0);

  // Read the cache on every render rather than mirroring it into state: that is what
  // makes switching back to an already-loaded node render instantly and never stale.
  const entry = node ? _cache.get(node) : undefined;

  const load = useCallback(
    async (target: string) => {
      setLoading(true);
      setError(null);
      try {
        await fetchSummary(target);
        bump();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load bring-up results");
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  // Lazy load: only hit the backend when there is nothing cached, or the cached
  // copy has aged past the refresh cadence.
  useEffect(() => {
    if (!node) return;
    const cached = _cache.get(node);
    const stale = !cached || (refreshMs > 0 && Date.now() - cached.fetchedAt >= refreshMs);
    if (!stale) return;
    // Fetch-on-change: load() owns loading/error as it syncs with the backend.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load(node);
  }, [node, refreshMs, load]);

  // Periodic refresh.
  useEffect(() => {
    if (!node || refreshMs <= 0) return;
    const id = window.setInterval(() => {
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      void load(node);
    }, refreshMs);
    return () => window.clearInterval(id);
  }, [node, refreshMs, load]);

  const refresh = useCallback(() => {
    if (!node) return;
    _cache.delete(node); // force a real fetch, not a cache hit
    void load(node);
  }, [node, load]);

  if (!node) {
    return { data: null, loading: false, error: null, lastUpdated: null, refresh };
  }
  return {
    data: entry?.data ?? null,
    loading,
    error,
    lastUpdated: entry?.fetchedAt ?? null,
    refresh,
  };
}
