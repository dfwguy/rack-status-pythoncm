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
/**
 * Active project/cluster context object and hook.
 *
 * No components here so React Refresh can hot-swap the provider; see ThemeContext.ts. Consumers
 * keep importing `useActiveCluster` from this path.
 */
import { createContext, useContext } from "react";
import type { ActiveSelection, Cluster, Platform, Project } from "../types/catalog";

export interface ActiveClusterState {
  projects: Project[];
  active: ActiveSelection;
  activeProject: Project | null;
  activeCluster: Cluster | null;
  activePlatform: Platform | null;
  aiContext: string;
  loading: boolean;
  error: string | null;
  /** Bumps whenever the active project/cluster changes (use as a refetch dep). */
  version: number;
  refresh: () => Promise<void>;
  setActive: (projectId: string, clusterId: string) => Promise<void>;
}

export const ActiveClusterContext = createContext<ActiveClusterState | undefined>(undefined);

export function useActiveCluster(): ActiveClusterState {
  const ctx = useContext(ActiveClusterContext);
  if (!ctx) {
    throw new Error("useActiveCluster must be used within an ActiveClusterProvider");
  }
  return ctx;
}
