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
  /**
   * Bumps whenever the catalog changes — the active project/cluster, *or* its contents.
   *
   * Use as a refetch dep. Contents count because compatibility is computed from them: a tool
   * registered for the active platform, a platform whose capabilities changed, a cluster moved
   * to a different platform all change which tools a board may offer.
   */
  version: number;
  refresh: () => Promise<void>;
  setActive: (projectId: string, clusterId: string) => Promise<void>;
  /**
   * Announce a write to the catalog, so every consumer of `version` refetches.
   *
   * Called by the screens that write it. Without this, `version` bumped only on a cluster
   * switch, so a tool registered in the Tool Registry did not appear in "Add action" until a
   * full page reload — the board had fetched its list on mount and had no reason to ask again.
   *
   * Explicit at each write site rather than folded into `refresh()`: `refresh` also runs on
   * mount, and bumping there would make all three catalog consumers fetch twice on every load
   * to serve a change that did not happen.
   */
  catalogChanged: () => void;
}

export const ActiveClusterContext = createContext<ActiveClusterState | undefined>(undefined);

export function useActiveCluster(): ActiveClusterState {
  const ctx = useContext(ActiveClusterContext);
  if (!ctx) {
    throw new Error("useActiveCluster must be used within an ActiveClusterProvider");
  }
  return ctx;
}
