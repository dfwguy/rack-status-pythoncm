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
// gridLayout — where a new terminal pane goes, and how big it is.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Kept separate from the view and free of React so the placement rules can be tested directly.
// The grid is otherwise fully draggable and resizable, so this only decides the *initial* shape;
// once an operator arranges panes by hand, their layout is preserved.
import type { LayoutItem } from "react-grid-layout";

/** react-grid-layout column count. 12 divides by 2, 3, 4 and 6, so every tiling below is exact. */
export const GRID_COLS = 12;

/** Row height in px. Pane height is rows * (ROW_HEIGHT + MARGIN) - MARGIN. */
export const ROW_HEIGHT = 30;
export const GRID_MARGIN = 8;

/** A pane shorter than this cannot show enough lines to be worth having. */
export const MIN_PANE_ROWS = 6;
/** Below roughly this width an 80-column shell starts wrapping at a readable font size. */
export const MIN_PANE_COLS = 3;

/**
 * No maximum: a pane may be resized to the full grid width.
 *
 * Dragging a second pane below the first leaves the first row half empty, and the obvious response
 * is to widen the remaining pane across it. Capping width at the auto-placement's half would make
 * that impossible and strand the empty column.
 */
export const MAX_PANE_COLS = GRID_COLS;

/**
 * Panes are laid out at most two across.
 *
 * Not an arbitrary cap: a shell is only useful at about 80 columns, and on a 1920px window three
 * across leaves each pane near 620px, which is where 80 columns stops fitting at a legible font
 * size. Two across stays comfortable on a laptop, and an operator who wants a third can drag one
 * -- the constraint is on the automatic placement, not on what the grid allows.
 */
export const MAX_AUTO_COLUMNS = 2;

/**
 * Default height in grid rows for an automatically placed pane.
 *
 * react-grid-layout sizes an item as ``h * ROW_HEIGHT + (h - 1) * marginY``, so 14 rows is
 * 14*30 + 13*8 = 524px. Less the 32px header and padding that leaves close to 30 lines of
 * terminal -- enough for `htop` or a `sinfo` listing without scrolling, and short enough that two
 * stacked panes still fit a laptop screen. Panes are resizable, so this is only a starting size.
 */
export const DEFAULT_PANE_ROWS = 14;

/**
 * Tile *count* panes: one full width, then two across with rows growing beneath.
 *
 * 1 -> 1x1, 2 -> 2x1, 3 and 4 -> 2x2, and on from there, which is the progression the feature was
 * specified with. A single pane spans the full width rather than sitting half-empty, since there is
 * nothing to sit beside it.
 */
export function autoLayout(ids: string[], paneRows = DEFAULT_PANE_ROWS): LayoutItem[] {
  if (ids.length === 0) return [];
  if (ids.length === 1) {
    return [{ i: ids[0], x: 0, y: 0, w: GRID_COLS, h: paneRows, minW: MIN_PANE_COLS, maxW: MAX_PANE_COLS, minH: MIN_PANE_ROWS }];
  }
  const width = GRID_COLS / MAX_AUTO_COLUMNS;
  return ids.map((id, index) => ({
    i: id,
    x: (index % MAX_AUTO_COLUMNS) * width,
    y: Math.floor(index / MAX_AUTO_COLUMNS) * paneRows,
    w: width,
    h: paneRows,
    minW: MIN_PANE_COLS,
    maxW: MAX_PANE_COLS,
    minH: MIN_PANE_ROWS,
  }));
}

/**
 * Place one new pane without disturbing the panes already there.
 *
 * Re-tiling everything on each add would throw away an arrangement the operator built by hand,
 * so an existing layout is preserved and the newcomer fills a gap. The exception is going from one
 * pane to two: the first pane is full width because it was alone, and it has to halve to make room
 * -- otherwise the second pane is pushed onto its own row and the result is not the 2x1 asked for.
 */
export function appendToLayout(
  current: LayoutItem[],
  newId: string,
  paneRows = DEFAULT_PANE_ROWS,
): LayoutItem[] {
  if (current.length === 0) return autoLayout([newId], paneRows);

  if (current.length === 1 && current[0].w === GRID_COLS) {
    const half = GRID_COLS / MAX_AUTO_COLUMNS;
    return [
      { ...current[0], x: 0, y: 0, w: half },
      { i: newId, x: half, y: 0, w: half, h: current[0].h, minW: MIN_PANE_COLS, maxW: MAX_PANE_COLS, minH: MIN_PANE_ROWS },
    ];
  }

  // Right of the last pane if that half is free, otherwise start a new row. Reading the actual
  // geometry rather than the count keeps this correct after a manual rearrangement.
  const half = GRID_COLS / MAX_AUTO_COLUMNS;
  const last = current.reduce((a, b) => (b.y > a.y || (b.y === a.y && b.x > a.x) ? b : a));
  const rowOccupied = current.filter((l) => l.y === last.y);
  const usedWidth = rowOccupied.reduce((sum, l) => sum + l.w, 0);

  if (usedWidth + half <= GRID_COLS) {
    return [
      ...current,
      { i: newId, x: usedWidth, y: last.y, w: half, h: last.h, minW: MIN_PANE_COLS, maxW: MAX_PANE_COLS, minH: MIN_PANE_ROWS },
    ];
  }
  const nextY = Math.max(...current.map((l) => l.y + l.h));
  return [
    ...current,
    { i: newId, x: 0, y: nextY, w: half, h: paneRows, minW: MIN_PANE_COLS, maxW: MAX_PANE_COLS, minH: MIN_PANE_ROWS },
  ];
}

// Shared with the tool-widget board: the echo behaviour it guards against is the library's,
// not the terminal's. Re-exported so this module stays the one import for pane placement.
export { sameLayout } from "../../lib/gridLayoutCore";

/**
 * Drop a pane and close the vertical gap it leaves.
 *
 * react-grid-layout compacts vertically on its own, but only once it re-renders; doing it here
 * keeps the persisted layout honest so a reload does not restore a hole.
 */
export function removeFromLayout(current: LayoutItem[], id: string): LayoutItem[] {
  const kept = current.filter((l) => l.i !== id);
  if (kept.length === 1) {
    // Back to a single pane: give it the full width it would have had on its own.
    return [{ ...kept[0], x: 0, y: 0, w: GRID_COLS }];
  }
  return kept;
}
