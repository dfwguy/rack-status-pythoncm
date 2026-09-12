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
// useCatalogTools — the tools the active cluster can run, for widget boards.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Fetched WITHOUT a surface filter on purpose. A board needs two lists from one request: the
// tools seeded onto its own surface (the default board) and every other tool the cluster can
// run (what "Add action" offers). Asking the server per surface would give the first and hide
// the second, so the surface split happens in `widgetLayout`.
import { useCallback, useEffect, useState } from "react";
import { listTools } from "../services/catalogService";
import { useActiveCluster } from "../context/ActiveClusterContext";
import type { Tool } from "../types/catalog";

export interface CatalogToolsState {
  tools: Tool[];
  loading: boolean;
  error: string | null;
  reload: () => void;
}

export function useCatalogTools(): CatalogToolsState {
  const { activeCluster, version } = useActiveCluster();
  const clusterId = activeCluster?.id;
  const [tools, setTools] = useState<Tool[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    // The cluster can change while a fetch is in flight; a late response for the previous
    // cluster would otherwise offer tools that platform cannot run.
    let live = true;
    setLoading(true);
    listTools(clusterId)
      .then((items) => {
        if (!live) return;
        setTools(items);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!live) return;
        setTools([]);
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [clusterId, version, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { tools, loading, error, reload };
}
