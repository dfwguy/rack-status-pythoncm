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
// gridState — the terminal grid's state shape and the operations on it.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Separate from TerminalGrid.tsx on purpose: a module that exports both a component and plain
// helpers costs React Refresh a full page reload on every edit, which is the same reason
// parseNodesFromRack lives apart from its callers.
import type { LayoutItem } from "react-grid-layout";
import type { TerminalPaneSpec } from "./TerminalPane";
import { appendToLayout } from "./gridLayout";

export interface TerminalGridState {
  panes: TerminalPaneSpec[];
  layout: LayoutItem[];
}

export const EMPTY_GRID: TerminalGridState = { panes: [], layout: [] };

/** Add a pane and place it, keeping the existing arrangement intact. */
export function addPane(
  state: TerminalGridState,
  spec: Omit<TerminalPaneSpec, "id">,
): TerminalGridState {
  // Unique per pane, not per target: opening two shells on one node is legitimate (one tailing a
  // log, one running commands) and keying on the hostname would collide.
  const id = `t${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  return {
    panes: [...state.panes, { ...spec, id }],
    layout: appendToLayout(state.layout, id),
  };
}

/**
 * Record the backend session id for a pane.
 *
 * Returns the same object when nothing changed, so a repeated report from a pane cannot drive an
 * endless render loop through the parent's state.
 */
export function withSession(
  state: TerminalGridState,
  paneId: string,
  sessionId: string,
): TerminalGridState {
  const pane = state.panes.find((p) => p.id === paneId);
  if (!pane || pane.session === sessionId) return state;
  return {
    ...state,
    panes: state.panes.map((p) => (p.id === paneId ? { ...p, session: sessionId } : p)),
  };
}
