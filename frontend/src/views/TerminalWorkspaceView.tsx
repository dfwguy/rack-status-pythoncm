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
// TerminalWorkspaceView — Tools > Terminal: a grid of live shells inside the app shell.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Replaces the mock preview that used to sit here. That page showed hardcoded sample output and
// linked out to the standalone /terminal tab, so the only way to watch two nodes at once was to
// juggle browser tabs -- which is most of what this page is for.
//
// Layout and pane list are persisted per cluster, because an operator's arrangement is part of
// their working set: reloading the dashboard should not close their shells' positions. The
// sessions themselves do not survive a reload (the WebSocket goes with the page), so panes are
// restored as fresh connections to the same targets.
import { useCallback, useEffect, useMemo, useState } from "react";
import Box from "@mui/material/Box";
import { Button, Text, Tooltip } from "@nvidia/foundations-react-core";
import { ExternalLink, Plus, Trash2 } from "lucide-react";
import { PageHeader } from "../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { TerminalGrid } from "../components/terminal/TerminalGrid";
import { EMPTY_GRID, addPane, withSession, type TerminalGridState } from "../components/terminal/gridState";
import { AddTerminalDialog, type TerminalTarget } from "../components/terminal/AddTerminalDialog";
import { detachUrl } from "../components/terminal/detachState";
import { openApiUrl } from "../lib/apiFetch";
import { useContainerWidth } from "react-grid-layout";
import { useActiveCluster } from "../context/ActiveClusterContext";

const FALLBACK_TRAYS_PER_RACK = 18;
const STORAGE_PREFIX = "nvis-terminal-grid";

interface TerminalWorkspaceViewProps {
  logDir?: string;
}

/** Sessions belong to a page load, so they are dropped on restore rather than dangled. */
function loadSaved(key: string): TerminalGridState | null {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as TerminalGridState;
    if (!Array.isArray(parsed?.panes) || parsed.panes.length === 0) return null;
    return {
      panes: parsed.panes.map((p) => ({ ...p, session: undefined })),
      layout: Array.isArray(parsed.layout) ? parsed.layout : [],
    };
  } catch {
    return null;
  }
}

export default function TerminalWorkspaceView({ logDir }: TerminalWorkspaceViewProps) {
  const { activeCluster, activePlatform } = useActiveCluster();
  const traysPerRack =
    activeCluster?.geometry?.trays_per_rack ||
    activePlatform?.geometry?.trays_per_rack ||
    FALLBACK_TRAYS_PER_RACK;

  // Scoped per cluster: the same pane list on a different cluster would name nodes that do not
  // exist there.
  const storageKey = useMemo(
    () => `${STORAGE_PREFIX}:${activeCluster?.id ?? "default"}`,
    [activeCluster?.id],
  );

  const [state, setState] = useState<TerminalGridState>(EMPTY_GRID);
  const [pickerOpen, setPickerOpen] = useState(false);
  // The library's own measured-width hook: react-grid-layout needs an explicit pixel width
  // and cannot derive one. `mounted` distinguishes "not measured yet" from a genuine zero,
  // so the grid is not laid out against the placeholder width.
  // initialWidth carries the first paint; the ResizeObserver corrects it and xterm refits.
  const { width, containerRef: gridRef } = useContainerWidth({ initialWidth: 1280 });

  // Restore when the cluster changes, which also covers first mount.
  useEffect(() => {
    setState(loadSaved(storageKey) ?? EMPTY_GRID);
  }, [storageKey]);

  useEffect(() => {
    try {
      if (state.panes.length === 0) localStorage.removeItem(storageKey);
      else localStorage.setItem(storageKey, JSON.stringify(state));
    } catch {
      // A full or blocked localStorage must not take the terminal down with it.
    }
  }, [state, storageKey]);

  const onSession = useCallback((paneId: string, sessionId: string) => {
    setState((prev) => withSession(prev, paneId, sessionId));
  }, []);

  const add = useCallback((t: TerminalTarget) => {
    setState((prev) => addPane(prev, { target: t.target, mode: t.mode, label: t.label }));
  }, []);

  const detachAll = useCallback(() => {
    if (state.panes.length === 0) return;
    void openApiUrl(detachUrl(state));
  }, [state]);

  const closeAll = useCallback(() => setState(EMPTY_GRID), []);

  const count = state.panes.length;

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}>
      <PageHeader
        contentId="terminal"
        title="Terminal"
        description={
          count === 0
            ? "Interactive head-node and compute shells, side by side in one workspace."
            : `${count} shell${count === 1 ? "" : "s"} open. Drag a pane by its header to rearrange; drag its right or bottom edge to resize.`
        }
      />

      {/* The page's own actions sit below the heading rather than in the header's action slot,
          which is reserved for the app-wide Ask AI and Help controls. Crowding page actions in
          beside them makes a destructive "Close all" a neighbour of "Help". */}
      <Box sx={{ display: "flex", gap: 1, alignItems: "center", mt: 1, flexWrap: "wrap" }}>
        <Button
          kind="primary"
          color="brand"
          size="small"
          onClick={() => setPickerOpen(true)}
        >
          <Plus size={14} style={{ marginRight: 6 }} />
          Add terminal
        </Button>
        <Tooltip content="Move every terminal into its own browser tab">
          <Button kind="secondary" size="small" onClick={detachAll} disabled={count === 0}>
            <ExternalLink size={14} style={{ marginRight: 6 }} />
            Detach grid
          </Button>
        </Tooltip>
        <Tooltip content="Disconnect and close every terminal">
          <Button
            kind="secondary"
            color="danger"
            size="small"
            onClick={closeAll}
            disabled={count === 0}
          >
            <Trash2 size={14} style={{ marginRight: 6 }} />
            Close all
          </Button>
        </Tooltip>
      </Box>

      <Box ref={gridRef} sx={{ mt: 2 }}>
        <TerminalGrid
          panes={state.panes}
          layout={state.layout}
          width={width}
          onChange={setState}
          onSession={onSession}
        />
      </Box>

      {count > 0 && (
        <Text
          kind="body/regular/sm"
          style={{ display: "block", marginTop: 12, color: "var(--text-color-secondary)" }}
        >
          Shells run over the same SSH path as the rest of the dashboard. Closing a pane
          disconnects it; a page reload reopens the panes but not their sessions.
        </Text>
      )}

      <AddTerminalDialog
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        onAdd={add}
        logDir={logDir}
        traysPerRack={traysPerRack}
      />
    </Box>
  );
}
