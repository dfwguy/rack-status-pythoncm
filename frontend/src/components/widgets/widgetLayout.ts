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
// widgetLayout — which tool widgets are on a board, where they sit, and what the operator
// changed about that.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Free of React so the rules can be tested directly. The load-bearing decision here is that a
// customisation is stored as a DIFF against the catalog's default board, never as a snapshot
// of it. A snapshot would freeze the board on the day it was customised: an operator who had
// ever touched the layout would stop seeing tools added to the catalog afterwards, which is
// precisely the staleness this feature exists to remove. Storing "I added X, I hid Y" means a
// new catalog tool reaches everyone, customised or not.
import type { LayoutItem } from "react-grid-layout";
import type { BoardAction } from "./boardActions";

/** Gap between cards, horizontally and vertically. */
export const WIDGET_MARGIN = 12;

/**
 * Fixed card footprint. One grid column is exactly one card, so a span is a card count.
 *
 * 340 rather than 300: it gives the title and the single-line description noticeably more
 * room before they ellipsise, which is where most of the truncation was. Three across plus
 * gaps comes to 1044px, which still fits the content column beside the nav on a 1440-wide
 * window — the ceiling worth remembering, since a wider card drops the board to two across
 * rather than shrinking.
 */
export const WIDGET_CARD_WIDTH = 340;
// One number, two meanings, and the difference is worth knowing before you change it:
//   - reading the board, it is the *minimum* column width handed to KUI `Grid colMinWidth`.
//     The browser fits as many 340px columns as the content box holds and then shares the
//     remainder between them, so a card is 340 *or wider* and the count has no ceiling.
//   - customising it, it is the *exact* card width, because react-grid-layout is given
//     `gridWidth(cols)` and divides it into equal columns.
// So `MAX_ACROSS`, `cardsAcross` and `gridWidth` below all belong to Customise mode alone.

/**
 * The card's vertical budget, in px, itemised.
 *
 * Mostly *not* ours: `Card` owns its own layout, and at this app's density (no `density` prop
 * on the ThemeProvider, so the standard scale) `.nv-card-content` contributes 24px of padding
 * per side from `--spacing-density-2xl`. The budget exists so the fixed height below cannot
 * drift under what the card actually needs — which is the mistake that clipped the button row
 * off two earlier versions.
 *
 * `descriptionLines: 1` is the design, not a limit we ran into: a card is a label, a picture
 * and a button, with the full text on hover and the whole document one click away in Details.
 *
 * Only Customise mode needs these now. A CSS grid row sizes itself to its tallest item and
 * stretches the rest to match, so reading the board cannot clip a card no matter what this
 * says — the budget is here for react-grid-layout, which positions absolutely and will happily
 * draw a card over the row beneath it.
 */
/**
 * KUI's own media aspect ratio: `.nv-card-media { aspect-ratio: 400/234 }`.
 *
 * Recorded, and then deliberately overridden for our cards — see `media` below. Kept as a
 * named constant because the number this file *does* use only makes sense against it.
 */
export const MEDIA_ASPECT = 400 / 234;

export const CARD_METRICS = {
  /**
   * Banner height. Pinned, not derived from the aspect ratio, and this is a design decision
   * with two reasons behind it.
   *
   * The first is arithmetic. KUI's ratio makes the banner a fixed *proportion* of the card,
   * but a CSS grid column is much wider than the 340px Customise mode uses — three across a
   * 1450px pane is ~475 each, where the ratio yields a 278px banner and the artwork becomes
   * the majority of the card. Pinning the height means widening the window widens the picture
   * rather than growing the whole board.
   *
   * The second is that the height has to be *knowable*. `.nv-card-media` and
   * `.nv-card-content` both carry `flex: 1`, so an indeterminate banner leaves slack for the
   * content box to absorb — and `.nv-card-content` is `align-items: flex-start`, which parks
   * the text at its top and leaves the slack in a void under the buttons. A definite banner
   * height is what makes the card's total height equal its contents.
   *
   * 160 rather than the 199 the ratio gives at 340px: shorter cards, and at grid widths the
   * crop is light enough that the artwork still reads.
   */
  media: 160,
  contentPaddingY: 48, // --spacing-density-2xl, top and bottom
  title: 22,
  subtitle: 18,
  description: 20,
  rowGaps: 12, // Flex gap="1" between the four content lines
  /** 8 margin + a small KUI button; generous because the button height is KUI's, not ours. */
  buttonRow: 44,
} as const;

/** The shortest card that fits its own contents without clipping the button row. */
export function minimumCardHeight(): number {
  const m = CARD_METRICS;
  return (
    m.media +
    m.contentPaddingY +
    m.title +
    m.subtitle +
    m.description +
    m.rowGaps +
    m.buttonRow
  );
}

/** Fixed card height — derived, so it cannot fall below the budget above. */
export const WIDGET_CARD_HEIGHT = minimumCardHeight() + 8;

/** A grid row is exactly one card tall. */
export const WIDGET_ROW_HEIGHT = WIDGET_CARD_HEIGHT;

/** Most cards across, regardless of how wide the window gets. */
export const MAX_ACROSS = 3;

/**
 * The narrowest measurement we believe, and what we assume when we do not believe it.
 *
 * react-grid-layout needs a pixel width, so the board measures its container — and a measured
 * width can be wrong in a way a CSS layout never is. `useContainerWidth` does
 * `parseFloat(getComputedStyle(node).width)`, which on a container whose computed width is a
 * *percentage* yields the numeric part: `width: 100%` measures as **100px**. A real browser
 * resolves percentages to pixels before we see them, so this is mostly a test-environment
 * artefact — but "mostly" is doing real work in that sentence, and the consequence is bad
 * either way: a 100px board is one card across, which now also flips every card to the
 * horizontal orientation.
 *
 * 320 because that is about the narrowest real viewport (an iPhone SE), so anything below it
 * is a measurement we have no business trusting rather than a device we should lay out for.
 */
export const MIN_MEASURED_WIDTH = 320;

/** Matches react-grid-layout's own `initialWidth`, so pre-measurement paint agrees with it. */
export const ASSUMED_WIDTH = 1280;

export function usableWidth(measured: number): number {
  return Number.isFinite(measured) && measured >= MIN_MEASURED_WIDTH ? measured : ASSUMED_WIDTH;
}

// --- orientation ------------------------------------------------------------------------

export type CardOrientation = "vertical" | "horizontal";

/**
 * A horizontal card puts the media in a left-hand column instead of a banner on top, so its
 * height is the content budget alone — roughly half a vertical card.
 *
 * `mediaWidth` rather than a height: the artwork is 16:9 and gets cropped either way, and a
 * fixed column keeps the text block starting at the same x on every card in the stack.
 */
export const HORIZONTAL_CARD_METRICS = {
  /**
   * Narrowest the media column gets, on the narrowest card — a stacked card on a phone, where
   * anything wider leaves the text column unusable. index.css scales up from here with a
   * `clamp`, because the same class also dresses a standalone full-width card.
   */
  mediaWidth: 132,
  /** Widest it gets, so artwork cannot crowd out prose on an ultrawide window. */
  mediaWidthMax: 280,
  contentPaddingY: 48, // --spacing-density-2xl, top and bottom
  title: 22,
  subtitle: 18,
  description: 20,
  rowGaps: 12,
  buttonRow: 44,
} as const;

export function minimumHorizontalCardHeight(): number {
  const m = HORIZONTAL_CARD_METRICS;
  return m.contentPaddingY + m.title + m.subtitle + m.description + m.rowGaps + m.buttonRow;
}

export const WIDGET_CARD_HEIGHT_HORIZONTAL = minimumHorizontalCardHeight() + 8;

/**
 * Grid of vertical cards when there is room for a grid; a stack of horizontal ones when there
 * is not.
 *
 * This is KUI's own guidance rather than a preference: `vertical` is documented for cards that
 * "display in a grid, saving horizontal space", and `horizontal` for content-led cards that
 * "typically stack vertically". The two modes are for two different layouts, so the switch is
 * the layout changing, not a style toggle.
 *
 * What it buys on a phone or a narrow pane: a one-across grid of vertical cards is a tall
 * column of mostly-artwork, and nine of them is a lot of scrolling. The same nine as horizontal
 * cards are about half the height each, because the media sits beside the text instead of above
 * it.
 */
export function cardOrientation(containerWidth: number): CardOrientation {
  return cardsAcross(containerWidth) === 1 ? "horizontal" : "vertical";
}

/** Row height for the orientation in play. */
export function rowHeightFor(orientation: CardOrientation): number {
  return orientation === "horizontal" ? WIDGET_CARD_HEIGHT_HORIZONTAL : WIDGET_CARD_HEIGHT;
}

/**
 * How many cards fit across *at their fixed width*.
 *
 * This is the whole reason the board no longer uses 12 proportional columns: with `cols: 12`
 * and a span of 4, react-grid-layout derives the column width from the container, so cards
 * stretched with the window and "fixed width" was never true. Here one column *is* one card,
 * `gridWidth` below pins the container to an exact multiple, and the window only changes how
 * many fit — never how big they are.
 */
export function cardsAcross(containerWidth: number): number {
  const per = WIDGET_CARD_WIDTH + WIDGET_MARGIN;
  const fits = Math.floor((containerWidth + WIDGET_MARGIN) / per);
  return Math.max(1, Math.min(MAX_ACROSS, fits));
}

/**
 * The exact pixel width to hand react-grid-layout for *cols* cards.
 *
 * RGL computes `colWidth = (width - margin.x * (cols - 1)) / cols` when container padding is
 * zero, so feeding it this width makes every column exactly `WIDGET_CARD_WIDTH`.
 */
export function gridWidth(cols: number): number {
  return WIDGET_CARD_WIDTH * cols + WIDGET_MARGIN * (cols - 1);
}

/**
 * The width to hand react-grid-layout, for the orientation in play.
 *
 * Vertical keeps the fixed-multiple arithmetic above — that is what makes "fixed card width"
 * true rather than approximate. Horizontal deliberately does the opposite and fills the
 * container: a stacked content-led card has no reason to leave a gutter, and on a phone every
 * pixel of width is text that does not have to wrap.
 */
export function boardWidthFor(
  orientation: CardOrientation,
  cols: number,
  containerWidth: number,
): number {
  if (orientation === "vertical") return gridWidth(cols);
  // A floor so a zero measurement (first render, before the container is measured) does not
  // produce a zero-width grid that never recovers.
  return Math.max(280, Math.floor(containerWidth));
}

/**
 * Positions only. The tool document supplies everything else, so storing more would stale.
 *
 * Still used *inside* Customise mode, where react-grid-layout needs coordinates to drag. They
 * are no longer what gets **stored** — see `BoardCustomization.order`.
 */
export interface WidgetPosition {
  i: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

/**
 * An operator's changes to a surface's board, as a diff against the catalog default.
 *
 * `v` is checked on read: a shape change ships a new number and old records are discarded
 * rather than half-understood.
 */
export interface BoardCustomization {
  /**
   * 4 since an arrangement is an *order*, not a set of coordinates.
   *
   * Version history, because each bump cost operators their arrangement and the reasons are
   * worth not repeating:
   *
   * - **v1 → v2**: positions moved from 12 proportional columns (`w: 4`, `x: 0|4|8`) to one
   *   column per fixed-width card. `x: 8` would have meant the ninth card across, so those
   *   numbers were not rescalable and v1 records are discarded.
   * - **v2 → v3**: ids became namespaced keys (`nvloom` → `tool:nvloom`), because workflow ids
   *   and tool ids are independent spaces that can collide. This one *is* mechanical, so v2
   *   records are **migrated** rather than dropped — see `parseCustomization`. A second reset
   *   in the same week is avoidable, so it was avoided.
   * - **v3 → v4**: `layout: {x, y}[]` became `order: string[]`. The board is a CSS grid whose
   *   column count the *browser* chooses, so there is no coordinate space to store a position
   *   in: the same record is three across on a desktop and one on a phone. Storing what the
   *   operator actually expressed — "this card before that one" — is the only form that
   *   survives a resize. v3 records migrate losslessly by reading their positions in reading
   *   order, which is what an operator arranging cards meant anyway.
   */
  v: 4;
  /** Action keys the operator put on a board that does not include them by default. */
  added: string[];
  /** Action keys from the default board the operator hid. */
  removed: string[];
  /**
   * The operator's arrangement, first to last.
   *
   * A partial list is fine and expected: a key absent from it falls back to its catalog `order`
   * (see `resolveBoard`), so a tool seeded after the customisation was saved lands in its
   * intended place rather than at the end of a list it predates.
   */
  order: string[];
}

export const SCHEMA_VERSION = 4;

export const EMPTY_CUSTOMIZATION: BoardCustomization = { v: 4, added: [], removed: [], order: [] };

/** The board as the catalog ships it: actions claiming this surface, in `order`. */
export function defaultBoard(actions: BoardAction[], surface: string): BoardAction[] {
  return actions
    .filter((a) => a.surfaces.includes(surface))
    .sort((a, b) => a.order - b.order || a.name.localeCompare(b.name));
}

/**
 * Tools an operator may add to this surface: launchable, compatible, not already shown.
 *
 * Deliberately *not* limited to the surface. "Add more action buttons that bring more tools
 * to run from the UI" is the whole point — a tool seeded onto the multirack surface is still
 * a legitimate thing to want on a rack board, and the catalog already vetted that it runs
 * here by returning it for this cluster.
 */
export function addableActions(actions: BoardAction[], onBoard: BoardAction[]): BoardAction[] {
  const shown = new Set(onBoard.map((a) => a.key));
  return actions
    .filter((a) => !shown.has(a.key))
    .sort((a, b) => a.category.localeCompare(b.category) || a.name.localeCompare(b.name));
}

/**
 * Apply a customisation to the catalog's default board.
 *
 * Both directions are filtered against the live catalog, so a tool that was renamed, made
 * incompatible with this platform, or deleted simply drops out of a stored record instead of
 * leaving a button that 404s.
 */
export function resolveBoard(
  actions: BoardAction[],
  surface: string,
  custom: BoardCustomization | null,
): BoardAction[] {
  const base = defaultBoard(actions, surface);
  if (!custom) return base;
  const removed = new Set(custom.removed);
  const byKey = new Map(actions.map((a) => [a.key, a]));
  const kept = base.filter((a) => !removed.has(a.key));
  const added = custom.added
    .filter((key) => !removed.has(key) && !kept.some((a) => a.key === key))
    .map((key) => byKey.get(key))
    .filter((a): a is BoardAction => !!a);
  return applyOrder([...kept, ...added], custom.order);
}

/**
 * Put the operator's arrangement first, then everything they never placed.
 *
 * Stable rather than clever: a key the operator ordered keeps its rank; a key they did not
 * keeps the relative position the catalog gave it. That is what makes a *partial* order safe
 * to store, and it means a newly seeded tool appears near its catalog siblings instead of
 * being exiled to the end.
 */
export function applyOrder(actions: BoardAction[], order: readonly string[]): BoardAction[] {
  if (order.length === 0) return actions;
  const rank = new Map(order.map((key, index) => [key, index]));
  return actions
    .map((action, index) => ({ action, index }))
    .sort((a, b) => {
      const ra = rank.get(a.action.key);
      const rb = rank.get(b.action.key);
      if (ra !== undefined && rb !== undefined) return ra - rb;
      // An ordered card always precedes an unordered one, so dragging a card to the front
      // cannot be undone by a catalog `order` that happens to be lower.
      if (ra !== undefined) return -1;
      if (rb !== undefined) return 1;
      return a.index - b.index;
    })
    .map((entry) => entry.action);
}

/** Reading order of a dragged grid: top row left-to-right, then the next row. */
export function orderFromPositions(positions: readonly WidgetPosition[]): string[] {
  return [...positions].sort((a, b) => a.y - b.y || a.x - b.x).map((p) => p.i);
}

/**
 * Left-to-right, top-to-bottom placement, one card per column.
 *
 * *cols* is a card count, not a span: with a fixed card width there is no proportional
 * arithmetic left to get wrong.
 */
export function defaultLayout(ids: string[], cols: number): LayoutItem[] {
  const perRow = Math.max(1, cols);
  return ids.map((id, index) => ({
    i: id,
    x: index % perRow,
    y: Math.floor(index / perRow),
    w: 1,
    h: 1,
  }));
}

/**
 * Positions for the resolved board, in Customise mode.
 *
 * There is no stored-position branch any more, and that deletion is the point of v4: the board
 * arrives already in the operator's order, so flowing it into the current column count *is*
 * the arrangement. The old version had to clamp a saved `x` into the current row width and
 * stack unplaced cards below the lowest `y` — arithmetic that existed only because positions
 * were persisted in a coordinate space the window could change underneath them.
 */
export function layoutFor(ids: string[], cols: number): LayoutItem[] {
  return defaultLayout(ids, cols);
}

/** Drop everything but geometry before persisting. */
export function toPositions(layout: readonly LayoutItem[]): WidgetPosition[] {
  return layout.map(({ i, x, y, w, h }) => ({ i, x, y, w, h }));
}

/**
 * Record that a widget was hidden.
 *
 * A default-board tool goes on `removed`; one the operator had added is simply un-added. Both
 * are needed — pushing an added tool onto `removed` would make re-adding it a no-op, since
 * `resolveBoard` lets a removal win.
 */
export function hideWidget(custom: BoardCustomization, key: string): BoardCustomization {
  const wasAdded = custom.added.includes(key);
  return {
    ...custom,
    added: custom.added.filter((k) => k !== key),
    removed: wasAdded ? custom.removed : [...new Set([...custom.removed, key])],
    order: custom.order.filter((k) => k !== key),
  };
}

/** Record that a widget was added, clearing any earlier removal of the same tool. */
export function showWidget(custom: BoardCustomization, key: string): BoardCustomization {
  return {
    ...custom,
    removed: custom.removed.filter((k) => k !== key),
    added: custom.added.includes(key) ? custom.added : [...custom.added, key],
  };
}

/** Whether anything is stored at all — drives whether "Reset" is offered. */
export function isCustomized(custom: BoardCustomization | null): boolean {
  if (!custom) return false;
  return custom.added.length > 0 || custom.removed.length > 0 || custom.order.length > 0;
}

/**
 * Parse a persisted record, rejecting anything that is not a shape we understand.
 *
 * v2 and v3 are migrated rather than discarded. v2's bare ids were all tools, because a board
 * could not hold anything else, so prefixing them is lossless; v3's positions read back as an
 * order, which is what an operator dragging cards was expressing. v1 is still refused — its
 * positions were in a proportional coordinate system that does not convert.
 */
export function parseCustomization(raw: string | null): BoardCustomization | null {
  if (!raw) return null;
  try {
    // Typed as unknown-shaped on purpose: this is data from localStorage, so it may be any
    // version (or none). Declaring it as the current interface would make the migration
    // branches below look like dead code to the compiler, which is exactly what matters here.
    const parsed = JSON.parse(raw) as { v?: unknown } & Record<string, unknown>;
    if (parsed?.v === 2) return migrateLayoutRecord(parsed, true);
    if (parsed?.v === 3) return migrateLayoutRecord(parsed, false);
    if (parsed?.v !== SCHEMA_VERSION) return null;
    const strings = (v: unknown): string[] =>
      Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
    return {
      v: 4,
      added: strings(parsed.added),
      removed: strings(parsed.removed),
      order: strings(parsed.order),
    };
  } catch {
    return null;
  }
}

/**
 * Upgrade a positional record (v2 or v3) to an order.
 *
 * *namespaceIds* covers the v2 half: its ids were bare (`nvloom` → `tool:nvloom`). An id that
 * already carries a namespace is left alone, so applying this twice is a no-op.
 *
 * The positions become an order by reading the grid the way a person does — top row
 * left-to-right, then the next. A record with no positions still migrates, because `added` and
 * `removed` are worth keeping on their own.
 */
function migrateLayoutRecord(
  parsed: Record<string, unknown>,
  namespaceIds: boolean,
): BoardCustomization {
  const key = (id: unknown): string | null => {
    if (typeof id !== "string" || !id) return null;
    if (!namespaceIds) return id;
    return id.startsWith("tool:") || id.startsWith("workflow:") ? id : `tool:${id}`;
  };
  const keys = (v: unknown): string[] =>
    Array.isArray(v) ? v.map(key).filter((k): k is string => !!k) : [];
  const positions = (Array.isArray(parsed.layout) ? parsed.layout : [])
    .map((p) => {
      const raw = p as Partial<WidgetPosition>;
      const i = key(raw.i);
      if (!i || ![raw.x, raw.y].every((n) => typeof n === "number" && Number.isFinite(n))) {
        return null;
      }
      return { i, x: raw.x as number, y: raw.y as number, w: 1, h: 1 };
    })
    .filter((p): p is WidgetPosition => !!p);
  return {
    v: 4,
    added: keys(parsed.added),
    removed: keys(parsed.removed),
    order: orderFromPositions(positions),
  };
}

// --- where a customisation is stored ----------------------------------------------------

const STORAGE_PREFIX = "nvis-widget-board";

/**
 * The localStorage key for one surface's board.
 *
 * Here rather than in `ToolWidgetBoard` because that file exports a component and may not also
 * export a function without breaking fast refresh — and because this module already owns the
 * rest of the persistence: the schema, its migrations and the diff that gets written.
 *
 * Scoped per cluster as well as per surface: the same board on another cluster would offer
 * tools that platform is not compatible with, and a position saved against them is noise.
 */
export function boardStorageKey(clusterId: string | undefined, surface: string): string {
  return `${STORAGE_PREFIX}:${clusterId ?? "default"}:${surface}`;
}
