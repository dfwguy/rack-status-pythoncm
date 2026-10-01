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
// useBoardActions — everything a widget board can offer: catalog tools plus workflows.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Tools are fetched WITHOUT a surface filter on purpose. A board needs two lists from one
// request: the actions seeded onto its own surface (the default board) and every other action
// the cluster can run (what "Add action" offers). Asking the server per surface would give the
// first and hide the second, so the surface split happens in `widgetLayout`.
//
// A workflow list failure is not fatal. Workflows are an addition to a board, never part of a
// default one, so an unreachable workflow engine costs the operator the *option* of adding a
// flow — it must not blank the validation actions, which is what a single combined failure
// would have done.
import { useCallback, useEffect, useState } from "react";
import { listTools } from "../services/catalogService";
import { listWorkflows } from "../services/workflowService";
import { useActiveCluster } from "../context/ActiveClusterContext";
import { buildActions, type BoardAction } from "../components/widgets/boardActions";

export interface BoardActionsState {
  actions: BoardAction[];
  loading: boolean;
  /** Set when the *catalog* could not be read — the board cannot be built without it. */
  error: string | null;
  /** Set when workflows could not be read; the board still works without them. */
  workflowError: string | null;
  reload: () => void;
}

export function useBoardActions(): BoardActionsState {
  const { activeCluster, version } = useActiveCluster();
  const clusterId = activeCluster?.id;
  const [actions, setActions] = useState<BoardAction[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [workflowError, setWorkflowError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const [loadedContext, setLoadedContext] = useState<{
    clusterId: string | undefined;
    version: number;
  } | null>(null);

  useEffect(() => {
    // The cluster can change while a fetch is in flight; a late response for the previous
    // cluster would otherwise offer tools that platform cannot run.
    let live = true;
    setLoading(true);
    Promise.allSettled([listTools(clusterId), listWorkflows()])
      .then(([toolsResult, workflowsResult]) => {
        if (!live) return;
        setLoadedContext({ clusterId, version });
        const tools = toolsResult.status === "fulfilled" ? toolsResult.value : [];
        const workflows = workflowsResult.status === "fulfilled" ? workflowsResult.value : [];
        setActions(buildActions(tools, workflows));
        setError(
          toolsResult.status === "rejected" ? describe(toolsResult.reason) : null,
        );
        setWorkflowError(
          workflowsResult.status === "rejected" ? describe(workflowsResult.reason) : null,
        );
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [clusterId, version, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  // Effects run after render: never expose the previous cluster's actions during a switch.
  const current = loadedContext?.clusterId === clusterId && loadedContext?.version === version;
  return {
    actions: current ? actions : [],
    loading: !current || loading,
    error: current ? error : null,
    workflowError: current ? workflowError : null,
    reload,
  };
}

function describe(reason: unknown): string {
  return reason instanceof Error ? reason.message : String(reason);
}
