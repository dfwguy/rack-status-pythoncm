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
// ToolWidgetBoard — the launch actions for one surface, driven by the Tool catalog.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Two layouts, one card. Reading the board is a **CSS grid** (KUI `Grid colMinWidth`): the
// browser divides the real content box, so it reflows with the window and there is no measured
// number to be wrong. Customising it is **react-grid-layout**, which needs an explicit pixel
// width and absolute positions — the price of dragging, paid only while dragging.
//
// That split is the correction to how this started. The board handed react-grid-layout a
// measured width in *both* modes, which made it the one card grid in the app that could not
// reflow: `useContainerWidth` measures with `parseFloat(getComputedStyle(node).width)`, so a
// container computed as `width: 100%` measures as 100px, and a 100px board is one card across.
// A plausible-looking number that is not a width, deciding the whole layout. Everything else
// here — ToolRegistryView, PlatformCatalogView, ProjectsView — has always been
// `repeat(auto-fill, minmax(320px, 1fr))` and has never had the problem.
//
// Cards are never resizable, in either mode: a shell's useful size depends on what is running
// in it, but a board of unequal tiles is harder to scan than the fixed list it replaced.
//
// The board is catalog-first and operator-second. The default comes from the server, a
// customisation is a diff stored per browser (see widgetLayout), and Reset removes the diff
// rather than rewriting it — which is what keeps "reset" honest when the catalog has moved on.
// An arrangement is stored as an *order*, not as coordinates, because the column count belongs
// to the browser now: the same board is three across on a desktop and one on a phone.
import { useCallback, useEffect, useMemo, useState } from "react";
import GridLayout, { type Layout, type LayoutItem, useContainerWidth } from "react-grid-layout";
import Box from "@mui/material/Box";
import { Banner, Button, Grid } from "@nvidia/foundations-react-core";
import { Download, Plus, RotateCcw, Settings2, Upload } from "lucide-react";
import type { BoardAction } from "./boardActions";
import { sameLayout } from "../../lib/gridLayoutCore";
import { ToolWidget, WIDGET_DRAG_HANDLE } from "./ToolWidget";
import { AddWidgetDialog } from "./AddWidgetDialog";
import { ToolInfoDialog } from "./ToolInfoDialog";
import { ExportBoardDialog } from "./ExportBoardDialog";
import { ImportBoardDialog } from "./ImportBoardDialog";
import { bundleToCustomization, type BoardBundle, type BundleSource } from "./boardBundle";
import {
  EMPTY_CUSTOMIZATION,
  WIDGET_CARD_WIDTH,
  WIDGET_MARGIN,
  addableActions,
  boardStorageKey,
  boardWidthFor,
  cardOrientation,
  cardsAcross,
  layoutFor,
  orderFromPositions,
  rowHeightFor,
  usableWidth,
  hideWidget,
  isCustomized,
  parseCustomization,
  resolveBoard,
  defaultBoard,
  showWidget,
  toPositions,
  SCHEMA_VERSION,
  type BoardCustomization,
} from "./widgetLayout";
import "react-grid-layout/css/styles.css";

interface ToolWidgetBoardProps {
  surface: string;
  /** Every action the board may show: catalog tools for this cluster, plus workflows. */
  actions: BoardAction[];
  clusterId?: string;
  disabled?: boolean;
  /** Tool ids with a launch in flight. */
  runningIds?: ReadonlySet<string>;
  /** Per-widget veto with a reason — e.g. the node selection does not fit its bounds. */
  blockedReason?: (action: BoardAction) => string | null;
  onRun: (action: BoardAction) => void;
  /** Rendered above the grid when the board is empty by default (not by customisation). */
  emptyMessage?: string;
  /** Provenance stamped into an exported bundle, and compared on import. */
  bundleSource?: BundleSource;
  /** Re-read the catalog after an import wrote definitions. */
  onCatalogChanged?: () => void;
}

export function ToolWidgetBoard({
  surface,
  actions,
  clusterId,
  disabled,
  runningIds,
  blockedReason,
  onRun,
  emptyMessage,
  bundleSource,
  onCatalogChanged,
}: ToolWidgetBoardProps) {
  const storageKey = useMemo(() => boardStorageKey(clusterId, surface), [clusterId, surface]);
  const [custom, setCustom] = useState<BoardCustomization | null>(null);
  const [editing, setEditing] = useState(false);
  const [adding, setAdding] = useState(false);
  const [info, setInfo] = useState<BoardAction | null>(null);
  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);
  const { containerRef, width } = useContainerWidth();

  // Re-read on key change, not just on mount: switching cluster switches boards.
  useEffect(() => {
    try {
      setCustom(parseCustomization(localStorage.getItem(storageKey)));
    } catch {
      setCustom(null);
    }
    setEditing(false);
  }, [storageKey]);

  const persist = useCallback(
    (next: BoardCustomization | null) => {
      setCustom(next);
      try {
        if (next && isCustomized(next)) localStorage.setItem(storageKey, JSON.stringify(next));
        else localStorage.removeItem(storageKey);
      } catch {
        // A board that cannot be persisted still works for this session; storage being full
        // or blocked is not a reason to refuse to render the actions.
      }
    },
    [storageKey],
  );

  const board = useMemo(() => resolveBoard(actions, surface, custom), [actions, surface, custom]);
  // One believed width, derived once: an implausible measurement must not reach two decisions
  // and let them disagree. See `usableWidth` for how a measurement comes to be wrong.
  const measured = useMemo(() => usableWidth(width), [width]);
  // Too narrow for a grid means the cards stack, and a stacked card is horizontal: media in a
  // left column, about half the height. See `cardOrientation` for why that is KUI's rule and
  // not a preference.
  //
  // This is the *only* thing the measurement decides now. Sizing the board is the browser's
  // job (see the Grid below); orientation is a component prop, which CSS cannot set, so it is
  // the one decision that genuinely needs a number in JavaScript — and if that number is
  // wrong, `usableWidth` sends us to the vertical default rather than to a stacked board.
  const orientation = useMemo(() => cardOrientation(measured), [measured]);

  // --- Customise mode only, below. -------------------------------------------------------
  // react-grid-layout cannot take part in CSS layout: it needs an explicit pixel width and
  // absolute positions. That is a fair trade for dragging, and a bad one for reading, so it
  // is mounted only while `editing`.
  const cols = useMemo(() => cardsAcross(measured), [measured]);
  const boardWidth = useMemo(
    () => boardWidthFor(orientation, cols, measured),
    [orientation, cols, measured],
  );
  const layout = useMemo(() => layoutFor(board.map((a) => a.key), cols), [board, cols]);

  const onLayoutChange = useCallback(
    (next: Layout) => {
      // Only meaningful while dragging. The grid also emits this on mount and on every
      // re-normalise, and persisting those would write to localStorage on page load.
      if (!editing) return;
      if (sameLayout(layout, next)) return;
      // Coordinates in, order out: what the operator expressed by dragging a card is "before
      // that one", and that is the only part of a drag a browser-sized grid can honour.
      persist({
        ...(custom ?? EMPTY_CUSTOMIZATION),
        order: orderFromPositions(toPositions(next)),
      });
    },
    [editing, layout, custom, persist],
  );

  const remove = useCallback(
    (action: BoardAction) => persist(hideWidget(custom ?? EMPTY_CUSTOMIZATION, action.key)),
    [custom, persist],
  );

  const add = useCallback(
    (action: BoardAction) => {
      persist(showWidget(custom ?? EMPTY_CUSTOMIZATION, action.key));
      setAdding(false);
    },
    [custom, persist],
  );

  /**
   * Adopt an imported board as *this browser's* customisation.
   *
   * Expressed as a diff against the catalog default, like every other customisation: the
   * bundle's keys become `added` for anything the default board does not carry, and
   * `removed` for anything it does that the bundle omits. Storing it as an absolute list
   * would freeze the board and stop future catalog additions appearing — the same trap
   * `resolveBoard` avoids.
   */
  const applyBundle = useCallback(
    (bundle: BoardBundle) => {
      const defaults = defaultBoard(actions, surface).map((a) => a.key);
      persist({ v: SCHEMA_VERSION, ...bundleToCustomization(bundle, defaults) });
      setImporting(false);
    },
    [actions, surface, persist],
  );

  const addable = useMemo(() => addableActions(actions, board), [actions, board]);
  const customized = isCustomized(custom);

  // One card, rendered identically in both modes — so switching into Customise rearranges the
  // container and nothing else.
  const card = (action: BoardAction) => (
    <ToolWidget
      action={action}
      orientation={orientation}
      disabled={disabled}
      blockedReason={blockedReason?.(action) ?? null}
      running={runningIds?.has(action.key)}
      onRun={onRun}
      onInfo={setInfo}
      editing={editing}
      onRemove={remove}
    />
  );

  return (
    <Box ref={containerRef} sx={{ width: "100%" }}>
      <Box sx={{ display: "flex", alignItems: "center", gap: 1, mb: 1 }}>
        <Box sx={{ flex: 1 }} />
        {editing ? (
          <>
            <Button
              kind="secondary"
              color="neutral"
              size="small"
              disabled={addable.length === 0}
              onClick={() => setAdding(true)}
            >
              <Plus size={14} style={{ marginRight: 6 }} />
              Add action
            </Button>
            <Button
              kind="secondary"
              color="neutral"
              size="small"
              onClick={() => setExporting(true)}
              disabled={board.length === 0}
            >
              <Download size={14} style={{ marginRight: 6 }} />
              Export
            </Button>
            <Button
              kind="secondary"
              color="neutral"
              size="small"
              onClick={() => setImporting(true)}
            >
              <Upload size={14} style={{ marginRight: 6 }} />
              Import
            </Button>
            {customized ? (
              <Button kind="secondary" color="neutral" size="small" onClick={() => persist(null)}>
                <RotateCcw size={14} style={{ marginRight: 6 }} />
                Reset to default
              </Button>
            ) : null}
          </>
        ) : null}
        <Button
          kind={editing ? "primary" : "secondary"}
          color={editing ? "brand" : "neutral"}
          size="small"
          onClick={() => setEditing((v) => !v)}
        >
          <Settings2 size={14} style={{ marginRight: 6 }} />
          {editing ? "Done" : "Customize"}
        </Button>
      </Box>

      {editing ? (
        // The grid below is absolutely positioned by react-grid-layout, so it contributes no
        // margin of its own and any banner sits flush against the first row of cards.
        <Box sx={{ mb: 2 }}>
          <Banner status="info" kind="inline">
            Drag a card by its grip to rearrange, remove one with its ✕, or add any tool the
            catalog offers for this cluster. This layout is saved in this browser only — Reset
            restores the board the catalog ships.
          </Banner>
        </Box>
      ) : null}

      {board.length === 0 ? (
        <Banner status="info" kind="inline">
          {customized
            ? "Every action has been removed from this board. Use Customize → Reset to default to bring them back."
            : (emptyMessage ??
              "No catalog tools are bound to this surface for the active cluster. Add one in the Tool Registry.")}
        </Banner>
      ) : editing ? (
        <GridLayout
          className="nvis-widget-board"
          layout={layout}
          width={boardWidth}
          gridConfig={{
            // One column per card, so a span is a card count and the column width is exactly
            // WIDGET_CARD_WIDTH for the `width` above.
            cols,
            rowHeight: rowHeightFor(orientation),
            margin: [WIDGET_MARGIN, WIDGET_MARGIN],
            // Without this the whole board is inset by the margin and the first card sits
            // right of the headings above it.
            containerPadding: [0, 0],
          }}
          dragConfig={{ enabled: true, handle: `.${WIDGET_DRAG_HANDLE}` }}
          // Fixed-size cards, always. See the note at the top of this file.
          resizeConfig={{ enabled: false, handles: [] }}
          onLayoutChange={onLayoutChange}
        >
          {board.map((action) => (
            // No style here: react-grid-layout sizes this wrapper itself, and an earlier
            // version's `height: 100%` was a guess that fought it. `Card` fills it via its
            // own `height: 100%`.
            <div key={action.key}>{card(action)}</div>
          ))}
        </GridLayout>
      ) : (
        // The board as the operator reads it: a CSS grid the *browser* lays out.
        //
        // `colMinWidth` is KUI's documented way to say "reflow with the viewport" — the
        // browser divides the real content box, so there is no measurement to be wrong, no
        // stepped 1/2/3 ceiling, and the board reflows during a drag of the window rather
        // than after a ResizeObserver callback. It is also the convention the rest of this app
        // already follows (`repeat(auto-fill, minmax(320px, 1fr))` in ToolRegistryView and
        // three of its neighbours); the board was the one screen that had opted out.
        //
        // `density-lg` is 12px at this app's density — the same gap as the drag mode's margin,
        // so entering and leaving Customise does not shift the cards.
        <Grid className="nvis-widget-board" colMinWidth={WIDGET_CARD_WIDTH} gap="density-lg">
          {board.map((action) => (
            // Grid rows stretch by default, so cards in a row share a height for free. The
            // fixed heights in widgetLayout exist for the drag mode, which has no such luxury.
            <div key={action.key}>{card(action)}</div>
          ))}
        </Grid>
      )}

      <AddWidgetDialog
        open={adding}
        actions={addable}
        onClose={() => setAdding(false)}
        onAdd={add}
      />

      <ToolInfoDialog action={info} onClose={() => setInfo(null)} />

      <ExportBoardDialog
        open={exporting}
        onClose={() => setExporting(false)}
        surface={surface}
        board={board}
        source={bundleSource ?? { product_version: "", cluster: "", platform: "", customer: "" }}
      />

      <ImportBoardDialog
        open={importing}
        onClose={() => setImporting(false)}
        surface={surface}
        localTools={actions.filter((a) => a.tool).map((a) => a.tool!)}
        localWorkflows={actions.filter((a) => a.workflow).map((a) => a.workflow!)}
        localPlatform={bundleSource?.platform}
        onApplyLayout={applyBundle}
        onImported={() => onCatalogChanged?.()}
      />
    </Box>
  );
}

export type { LayoutItem };
