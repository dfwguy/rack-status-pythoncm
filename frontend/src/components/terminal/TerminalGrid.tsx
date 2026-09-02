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
// TerminalGrid — a draggable, resizable grid of live shells.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Shared by the in-app view and the detached tab, so both behave identically and there is one
// place where pane state, layout and session tracking live.
//
// The load-bearing detail is that panes must NOT be unmounted by a move or a resize. Each pane
// owns an SSH session; unmounting disposes xterm and closes the WebSocket, so a pane that
// remounted on drag would silently kill the shell. That is why layout items are keyed on a stable
// pane id and why the pane list and the layout are separate pieces of state -- react-grid-layout
// only transforms its children while dragging, so as long as the keys hold, the shells survive.
import { useCallback, useMemo } from "react";
import GridLayout, { type Layout, type LayoutItem } from "react-grid-layout";
import Box from "@mui/material/Box";
import { Text } from "@nvidia/foundations-react-core";
import { TerminalPane, PANE_DRAG_HANDLE, type TerminalPaneSpec } from "./TerminalPane";
import type { TerminalGridState } from "./gridState";
import { TERMINAL_BACKGROUND } from "../../hooks/useTerminal";
import {
  GRID_COLS,
  GRID_MARGIN,
  ROW_HEIGHT,
  autoLayout,
  removeFromLayout,
  sameLayout,
} from "./gridLayout";
import "react-grid-layout/css/styles.css";
import "react-resizable/css/styles.css";
// Loaded after the library CSS so its handle styling wins.
import "./terminalGrid.css";

interface TerminalGridProps {
  panes: TerminalPaneSpec[];
  layout: LayoutItem[];
  width: number;
  onChange: (next: TerminalGridState) => void;
  /** Records the backend session id for a pane, so a detach can carry the live shell. */
  onSession: (paneId: string, sessionId: string) => void;
}

export function TerminalGrid({ panes, layout, width, onChange, onSession }: TerminalGridProps) {
  const remove = useCallback(
    (id: string) => {
      onChange({
        panes: panes.filter((p) => p.id !== id),
        layout: removeFromLayout(layout, id),
      });
    },
    [panes, layout, onChange],
  );

  // Only positions change here, never the pane list — so this cannot remount a shell.
  //
  // Ignored when the arrangement is unchanged: the grid fires this on mount and on re-normalise
  // with a layout equal to the one it was handed, and the consumer persists what it receives. A
  // fresh object every time would mean a state update and a storage write per mount.
  const onLayoutChange = useCallback(
    (next: Layout) => {
      const copy = [...next];
      if (sameLayout(layout, copy)) return;
      onChange({ panes, layout: copy });
    },
    [panes, layout, onChange],
  );

  const effectiveLayout = useMemo(
    () => (layout.length === panes.length ? layout : autoLayout(panes.map((p) => p.id))),
    [layout, panes],
  );

  if (panes.length === 0) {
    return (
      <Box
        sx={{
          border: "1px dashed #30363d",
          borderRadius: 1,
          p: 6,
          textAlign: "center",
          background: TERMINAL_BACKGROUND,
        }}
      >
        <Text kind="body/regular/md" style={{ color: "#7d8590" }}>
          No terminals open. Use <strong>Add terminal</strong> to open a shell on the head node or
          any compute node.
        </Text>
      </Box>
    );
  }

  return (
    // react-grid-layout v2 groups what v1 exposed as flat props into config objects.
    <GridLayout
      className="nvis-terminal-grid"
      layout={effectiveLayout}
      width={width}
      // containerPadding defaults to `margin`, which inset the whole grid by 8px and left the
      // first pane sitting right of the page's own content edge — visibly out of line with the
      // heading and the toolbar above it. The gap *between* panes still comes from `margin`; this
      // only removes the outer inset, so column 0 starts where the rest of the page starts.
      gridConfig={{
        cols: GRID_COLS,
        rowHeight: ROW_HEIGHT,
        margin: [GRID_MARGIN, GRID_MARGIN],
        containerPadding: [0, 0],
      }}
      dragConfig={{
        enabled: true,
        // Only the pane header drags. Without this the whole terminal is a drag surface and
        // selecting output with the mouse becomes impossible.
        handle: `.${PANE_DRAG_HANDLE}`,
      }}
      // Resizing changes the container; useTerminal's ResizeObserver refits xterm to whatever it
      // becomes, so the shell reflows instead of being clipped.
      //
      // Handles on the bottom and right edges as well as the corner: widening a pane across the
      // empty half of its row is the common case (drag one pane below another and the row is left
      // half empty), and reaching for a single corner grip to do it is fiddly.
      resizeConfig={{ enabled: true, handles: ["se", "s", "e"] }}
      onLayoutChange={onLayoutChange}
    >
      {panes.map((spec) => (
        <div key={spec.id}>
          <TerminalPane spec={spec} onClose={remove} onSession={onSession} />
        </div>
      ))}
    </GridLayout>
  );
}
