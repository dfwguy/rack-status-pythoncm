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
/** Loads the active project/cluster from the catalog API; the hook lives in ActiveClusterContext.ts. */
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  getActiveContext,
  listProjects,
  setActiveContext,
} from "../services/catalogService";
import type { ActiveSelection, Cluster, Platform, Project } from "../types/catalog";
import { ActiveClusterContext, type ActiveClusterState } from "./ActiveClusterContext";

export function ActiveClusterProvider({ children }: { children: ReactNode }) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [active, setActiveSel] = useState<ActiveSelection>({ project: "", cluster: "" });
  const [activeProject, setActiveProject] = useState<Project | null>(null);
  const [activeCluster, setActiveCluster] = useState<Cluster | null>(null);
  const [activePlatform, setActivePlatform] = useState<Platform | null>(null);
  const [aiContext, setAiContext] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);

  const refresh = useCallback(async () => {
    try {
      const [ctx, projs] = await Promise.all([getActiveContext(), listProjects()]);
      setActiveSel(ctx.active);
      setActiveProject(ctx.project);
      setActiveCluster(ctx.cluster);
      setActivePlatform(ctx.platform);
      setAiContext(ctx.ai_context || "");
      setProjects(projs);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load catalog context");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const setActive = useCallback(
    async (projectId: string, clusterId: string) => {
      await setActiveContext({ project: projectId, cluster: clusterId });
      await refresh();
      setVersion((v) => v + 1);
    },
    [refresh],
  );

  const value = useMemo<ActiveClusterState>(
    () => ({
      projects,
      active,
      activeProject,
      activeCluster,
      activePlatform,
      aiContext,
      loading,
      error,
      version,
      refresh,
      setActive,
    }),
    [projects, active, activeProject, activeCluster, activePlatform, aiContext, loading, error, version, refresh, setActive],
  );

  return <ActiveClusterContext.Provider value={value}>{children}</ActiveClusterContext.Provider>;
}
