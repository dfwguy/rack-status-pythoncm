// TerminalGridTabView — the whole terminal grid, detached into its own browser tab.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Reached by the grid's Detach button, which encodes the panes into the URL. Renders the same
// TerminalGrid as the in-app view, so panes drag, resize, detach and close identically -- this
// view only supplies the full-window chrome and reads the handover payload.
//
// The panes arrive carrying their session ids, so this tab ADOPTS the shells that were already
// running rather than opening a second set. Detaching is a move, not a duplicate.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Banner, Text } from "@nvidia/foundations-react-core";
import { Terminal as TerminalIcon } from "lucide-react";
import { TerminalGrid } from "../components/terminal/TerminalGrid";
import { EMPTY_GRID, withSession, type TerminalGridState } from "../components/terminal/gridState";
import { decodeGrid } from "../components/terminal/detachState";
import { useContainerWidth } from "react-grid-layout";

export default function TerminalGridTabView() {
  // initialWidth carries the first paint; the ResizeObserver corrects it and xterm refits.
  const { width, containerRef: gridRef } = useContainerWidth({ initialWidth: 1280 });

  const initial = useMemo<TerminalGridState | null>(() => {
    const raw = new URLSearchParams(window.location.search).get("g");
    const decoded = decodeGrid(raw);
    return decoded ? { panes: decoded.panes, layout: decoded.layout } : null;
  }, []);

  const [state, setState] = useState<TerminalGridState>(
    initial ?? EMPTY_GRID,
  );

  useEffect(() => {
    const count = state.panes.length;
    document.title = count ? `Terminals (${count})` : "Terminals";
    // Full-bleed chrome: this tab is the terminal, so it owns the whole viewport.
    for (const el of [document.documentElement, document.body]) {
      el.style.margin = "0";
      el.style.padding = "0";
      el.style.height = "100%";
    }
    document.body.style.background = "#0d1117";
    const root = document.getElementById("root");
    if (root) root.style.height = "100%";
  }, [state.panes.length]);

  // Sessions can be reassigned here too: a pane whose original shell had already gone will get a
  // new id, and keeping it current means this tab can be detached from again.
  const onSession = useCallback((paneId: string, sessionId: string) => {
    setState((prev) => withSession(prev, paneId, sessionId));
  }, []);

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "#0d1117",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "6px 12px",
          background: "#161b22",
          borderBottom: "1px solid #30363d",
          flexShrink: 0,
        }}
      >
        <TerminalIcon size={16} color="#76b900" />
        <Text kind="body/bold/sm" style={{ color: "#e6edf3" }}>
          Terminals
        </Text>
        <Text kind="body/regular/sm" style={{ color: "#7d8590" }}>
          {state.panes.length} open — drag a header to rearrange, a corner to resize
        </Text>
      </div>

      <div ref={gridRef} style={{ flex: 1, padding: 8, minWidth: 0 }}>
        {!initial && (
          <Banner status="warning" kind="inline">
            This link carries no terminals — it may have been truncated when copied. Detach the grid
            again from Tools &rsaquo; Terminal.
          </Banner>
        )}
        {initial && (
          <TerminalGrid
            panes={state.panes}
            layout={state.layout}
            width={width}
            onChange={setState}
            onSession={onSession}
          />
        )}
      </div>
    </div>
  );
}
