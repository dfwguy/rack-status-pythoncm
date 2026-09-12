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
import { describe, expect, it } from "vitest";
import type { Tool } from "../../types/catalog";
import { fromTool } from "./boardActions";
import {
  ASSUMED_WIDTH,
  CARD_METRICS,
  EMPTY_CUSTOMIZATION,
  MAX_ACROSS,
  MIN_MEASURED_WIDTH,
  WIDGET_CARD_HEIGHT,
  WIDGET_CARD_HEIGHT_HORIZONTAL,
  WIDGET_CARD_WIDTH,
  WIDGET_MARGIN,
  addableActions,
  applyOrder,
  boardWidthFor,
  cardOrientation,
  cardsAcross,
  gridWidth,
  layoutFor,
  orderFromPositions,
  minimumCardHeight,
  minimumHorizontalCardHeight,
  rowHeightFor,
  usableWidth,
  defaultBoard,
  hideWidget,
  isCustomized,
  parseCustomization,
  resolveBoard,
  showWidget,
  toPositions,
} from "./widgetLayout";

const SURFACE = "rackDetail.validation";

function tool(id: string, over: Partial<Tool> = {}): Tool {
  return {
    id,
    name: id,
    category: "validation",
    script: `${id}.sh`,
    env_key: "",
    params: [],
    compatible: { match: "all", capabilities: [], platform_ids: [] },
    surfaces: [SURFACE],
    execution: { mode: "async", target: "rack" },
    description: "",
    ai_context: "",
    status: "ga",
    source: "seed",
    subtitle: "",
    button_label: "",
    icon: "",
    image: "",
    destructive: false,
    order: 100,
    launch: {
      kind: "async-job",
      endpoint: `/api/run-${id}-async`,
      preset_id: "",
      dialog: "",
      scopes: [],
      body: {},
      confirm: false,
      min_nodes: 0,
      max_nodes: 0,
    },
    ...over,
  };
}

/** A board action built from a catalog tool. Keys are namespaced: `tool:<id>`. */
function act(id: string, over: Partial<Tool> = {}) {
  return fromTool(tool(id, over));
}

describe("defaultBoard", () => {
  it("takes launchable tools for the surface, in catalog order", () => {
    const actions = [
      act("third", { order: 30 }),
      act("first", { order: 10 }),
      act("second", { order: 20 }),
    ];
    expect(defaultBoard(actions, SURFACE).map((t) => t.id)).toEqual(["first", "second", "third"]);
  });

  it("breaks an order tie on name, so the board does not shuffle between loads", () => {
    const actions = [act("beta", { order: 10 }), act("alpha", { order: 10 })];
    expect(defaultBoard(actions, SURFACE).map((t) => t.id)).toEqual(["alpha", "beta"]);
  });

  // Filtering unlaunchable tools moved to `buildActions` (boardActions.test.ts): a workflow has
  // no launch binding to inspect, so the board can no longer ask that question of every item.

  it("leaves out a tool seeded onto another surface", () => {
    const elsewhere = act("multirack-only", { surfaces: ["multirack"] });
    expect(defaultBoard([elsewhere, act("here")], SURFACE).map((t) => t.id)).toEqual(["here"]);
  });
});

describe("resolveBoard", () => {
  const actions = [
    act("preflight", { order: 10 }),
    act("stage1", { order: 20 }),
    act("offboard", { surfaces: [] }),
  ];

  it("is the catalog default when nothing is customised", () => {
    expect(resolveBoard(actions, SURFACE, null).map((t) => t.id)).toEqual(["preflight", "stage1"]);
  });

  it("hides a removed widget and appends an added one", () => {
    const custom = { v: 4 as const, added: ["tool:offboard"], removed: ["tool:preflight"], order: [] };
    expect(resolveBoard(actions, SURFACE, custom).map((t) => t.id)).toEqual(["stage1", "offboard"]);
  });

  it("shows a tool the catalog gained AFTER the board was customised", () => {
    // The reason a customisation is stored as a diff. With a snapshot, an operator who had
    // ever rearranged this board would never see the new NCCL script appear.
    const custom = { v: 4 as const, added: [], removed: ["tool:preflight"], order: [] };
    const withNewTool = [...actions, act("nccl-2.32", { order: 15 })];
    expect(resolveBoard(withNewTool, SURFACE, custom).map((t) => t.id)).toEqual([
      "nccl-2.32",
      "stage1",
    ]);
  });

  it("drops a stored id the catalog no longer serves", () => {
    const custom = { v: 4 as const, added: ["tool:deleted-tool"], removed: [], order: [] };
    expect(resolveBoard(actions, SURFACE, custom).map((t) => t.id)).toEqual(["preflight", "stage1"]);
  });

  it("does not duplicate a tool that was added and later seeded onto the surface", () => {
    const custom = { v: 4 as const, added: ["tool:stage1"], removed: [], order: [] };
    expect(resolveBoard(actions, SURFACE, custom).map((t) => t.id)).toEqual(["preflight", "stage1"]);
  });
});

describe("hideWidget / showWidget", () => {
  it("re-adding a hidden default clears the removal", () => {
    const hidden = hideWidget(EMPTY_CUSTOMIZATION, "tool:stage1");
    expect(hidden.removed).toContain("tool:stage1");
    const shown = showWidget(hidden, "tool:stage1");
    expect(shown.removed).not.toContain("tool:stage1");
  });

  it("hiding a tool the operator added un-adds it rather than recording a removal", () => {
    // Recording a removal instead would make re-adding it a silent no-op, because
    // resolveBoard lets a removal win over an addition.
    const added = showWidget(EMPTY_CUSTOMIZATION, "tool:extra");
    const hidden = hideWidget(added, "tool:extra");
    expect(hidden.added).not.toContain("tool:extra");
    expect(hidden.removed).not.toContain("tool:extra");
    expect(showWidget(hidden, "tool:extra").added).toContain("tool:extra");
  });

  it("forgets the rank of a widget it hides", () => {
    // Leaving a hidden card in the order would re-rank everything after it the moment the
    // operator added it back, which is not what "hide" said it would do.
    const custom = { v: 4 as const, added: [], removed: [], order: ["tool:a", "tool:b"] };
    expect(hideWidget(custom, "tool:a").order).toEqual(["tool:b"]);
  });
});

describe("layoutFor, the Customise-mode geometry", () => {
  it("tiles the board's order left-to-right, one card per column", () => {
    // No stored-position branch to test any more: the board arrives in the operator's order,
    // so flowing it into the current column count *is* the arrangement. The clamping and
    // below-the-lowest-y stacking this used to need only existed because positions were
    // persisted in a coordinate space the window could change underneath them.
    const layout = layoutFor(["a", "b", "c", "d"], 3);
    expect(layout.map((l) => [l.i, l.x, l.y])).toEqual([
      ["a", 0, 0],
      ["b", 1, 0],
      ["c", 2, 0],
      ["d", 0, 1],
    ]);
    expect(layout.every((l) => l.w === 1 && l.h === 1)).toBe(true);
  });

  it("reflows the same order into a single column", () => {
    expect(layoutFor(["a", "b"], 1).map((l) => [l.x, l.y])).toEqual([
      [0, 0],
      [0, 1],
    ]);
  });
});

describe("turning a drag into an order", () => {
  it("reads a dragged grid the way a person does: row by row", () => {
    const dragged = [
      { i: "c", x: 0, y: 1, w: 1, h: 1 },
      { i: "b", x: 1, y: 0, w: 1, h: 1 },
      { i: "a", x: 0, y: 0, w: 1, h: 1 },
    ];
    expect(orderFromPositions(dragged)).toEqual(["a", "b", "c"]);
  });

  it("survives the round trip a drag actually makes", () => {
    // The contract that makes v4 work: positions in, order out, order back to positions.
    const order = ["b", "a", "c"];
    expect(orderFromPositions(toPositions(layoutFor(order, 2)))).toEqual(order);
  });
});

describe("applyOrder", () => {
  it("ranks the cards the operator placed, in the order they placed them", () => {
    const board = [act("a", { order: 10 }), act("b", { order: 20 }), act("c", { order: 30 })];
    expect(applyOrder(board, ["tool:c", "tool:a"]).map((x) => x.id)).toEqual(["c", "a", "b"]);
  });

  it("leaves an unplaced card in its catalog position, after the placed ones", () => {
    // Why a partial order is safe to store: a tool seeded after the operator arranged this
    // board keeps its catalog rank relative to its siblings instead of being exiled.
    const board = [act("a", { order: 10 }), act("new", { order: 15 }), act("b", { order: 20 })];
    expect(applyOrder(board, ["tool:b"]).map((x) => x.id)).toEqual(["b", "a", "new"]);
  });

  it("is the catalog order when nothing was placed", () => {
    const board = [act("a", { order: 10 }), act("b", { order: 20 })];
    expect(applyOrder(board, []).map((x) => x.id)).toEqual(["a", "b"]);
  });

  it("ignores a rank for a card that is not on the board", () => {
    const board = [act("a", { order: 10 })];
    expect(applyOrder(board, ["tool:gone", "tool:a"]).map((x) => x.id)).toEqual(["a"]);
  });
});

describe("card sizing", () => {
  it("gives a card at least the height its own contents need", () => {
    // Two earlier versions hard-coded this below the real content height. A grid item is
    // absolutely positioned, so a card taller than its cell does not clip — it draws over the
    // row beneath, and shorts the container's computed height by the same amount per row so
    // the section below the board is overlapped too. Both symptoms, one number.
    expect(WIDGET_CARD_HEIGHT).toBeGreaterThanOrEqual(minimumCardHeight());
  });

  it("leaves room for the button row after the banner and the three text lines", () => {
    const m = CARD_METRICS;
    const aboveButtons = m.media + m.contentPaddingY + m.title + m.subtitle + m.description;
    expect(WIDGET_CARD_HEIGHT - aboveButtons).toBeGreaterThanOrEqual(m.buttonRow);
  });
});

describe("fixed-width columns", () => {
  it("fits as many whole cards as the width allows, capped at three", () => {
    // Derived from the constants, not hard-coded: these boundaries move whenever the card
    // width does, and a literal here would just fail the next time it changes.
    const exactly = (n: number) => gridWidth(n);
    for (const n of [1, 2, 3]) {
      expect(cardsAcross(exactly(n))).toBe(n);
      // One pixel short of fitting the nth card still shows n-1.
      expect(cardsAcross(exactly(n) - 1)).toBe(Math.max(1, n - 1));
    }
    expect(cardsAcross(exactly(MAX_ACROSS) * 4)).toBe(MAX_ACROSS);
  });

  it("never reports zero, so the grid always has at least one column", () => {
    expect(cardsAcross(0)).toBe(1);
    expect(cardsAcross(10)).toBe(1);
  });

  it("derives a width that makes each react-grid-layout column exactly one card", () => {
    // RGL: colWidth = (width - margin.x * (cols - 1)) / cols with zero container padding.
    // This is the arithmetic that makes 'fixed width' true rather than approximate.
    for (const cols of [1, 2, 3]) {
      const colWidth = (gridWidth(cols) - WIDGET_MARGIN * (cols - 1)) / cols;
      expect(colWidth).toBe(WIDGET_CARD_WIDTH);
    }
  });
});

describe("addableActions", () => {
  it("offers launchable tools from any surface that are not already shown", () => {
    const actions = [act("shown"), act("other-surface", { surfaces: ["multirack"] })];
    const addable = addableActions(actions, [actions[0]]);
    expect(addable.map((a) => a.id)).toEqual(["other-surface"]);
  });
});

describe("persistence", () => {
  it("keeps only geometry", () => {
    expect(toPositions([{ i: "a", x: 1, y: 2, w: 4, h: 1, minW: 2, static: true }])).toEqual([
      { i: "a", x: 1, y: 2, w: 4, h: 1 },
    ]);
  });

  it("treats an untouched board as not customised, so Reset is not offered", () => {
    expect(isCustomized(null)).toBe(false);
    expect(isCustomized(EMPTY_CUSTOMIZATION)).toBe(false);
    expect(isCustomized(showWidget(EMPTY_CUSTOMIZATION, "tool:a"))).toBe(true);
  });

  it("rejects a record from a different schema version rather than half-reading it", () => {
    // v1 stored 12-column spans; one column is now one card, so those numbers are not
    // rescalable and the record is discarded rather than reinterpreted.
    expect(parseCustomization(JSON.stringify({ v: 1, added: ["tool:a"] }))).toBeNull();
  });

  it("rejects malformed JSON and missing records", () => {
    expect(parseCustomization("{not json")).toBeNull();
    expect(parseCustomization(null)).toBeNull();
  });

  it("drops junk entries instead of failing the whole record", () => {
    const parsed = parseCustomization(
      JSON.stringify({
        v: 4,
        added: ["tool:good", 42],
        removed: null,
        order: ["tool:a", 7, null],
      }),
    );
    expect(parsed).toEqual({ v: 4, added: ["tool:good"], removed: [], order: ["tool:a"] });
  });

  it("migrates a v2 record by namespacing its bare tool ids", () => {
    // Lossless: a v2 board could only hold catalog tools, so every id in one was a tool id.
    // Migrating rather than discarding avoids resetting an operator's board a second time.
    const parsed = parseCustomization(
      JSON.stringify({
        v: 2,
        added: ["extra"],
        removed: ["stage1"],
        layout: [{ i: "stage1", x: 1, y: 0, w: 1, h: 1 }],
      }),
    );
    expect(parsed).toEqual({
      v: 4,
      added: ["tool:extra"],
      removed: ["tool:stage1"],
      order: ["tool:stage1"],
    });
  });

  it("does not double-prefix a key that is already namespaced", () => {
    const parsed = parseCustomization(
      JSON.stringify({ v: 2, added: ["workflow:wf1", "tool:a"], removed: [], layout: [] }),
    );
    expect(parsed?.added).toEqual(["workflow:wf1", "tool:a"]);
  });

  it("round-trips a customisation", () => {
    const custom = {
      v: 4 as const,
      added: ["tool:extra"],
      removed: ["tool:stage1"],
      order: ["tool:extra", "tool:stage2"],
    };
    expect(parseCustomization(JSON.stringify(custom))).toEqual(custom);
  });
});

describe("orientation, and the width the board is allowed to believe", () => {
  it("stacks horizontal cards only when a grid no longer fits", () => {
    // The pairing is the point: one card across *is* the stacked layout, and a stacked card is
    // horizontal. Two separate thresholds would let the board show a one-column grid of
    // vertical cards, which is the tall scroll this exists to avoid.
    expect(cardsAcross(360)).toBe(1);
    expect(cardOrientation(360)).toBe("horizontal");
    for (const w of [700, 1044, 1440, 2560]) {
      expect(cardsAcross(w)).toBeGreaterThan(1);
      expect(cardOrientation(w)).toBe("vertical");
    }
  });

  it("gives a horizontal card a shorter row, and never shorter than its contents", () => {
    // Media beside the text rather than above it is the whole saving; if this ever stopped
    // being shorter, the stacked layout would cost more scrolling than the grid it replaced.
    expect(rowHeightFor("horizontal")).toBeLessThan(rowHeightFor("vertical"));
    expect(WIDGET_CARD_HEIGHT_HORIZONTAL).toBeGreaterThanOrEqual(minimumHorizontalCardHeight());
    expect(WIDGET_CARD_HEIGHT).toBeGreaterThanOrEqual(minimumCardHeight());
  });

  it("fills the container when stacked and keeps exact card multiples when gridded", () => {
    expect(boardWidthFor("vertical", 3, 1440)).toBe(gridWidth(3));
    expect(boardWidthFor("horizontal", 1, 390)).toBe(390);
  });

  it("disbelieves a measurement that is really a percentage", () => {
    // react-grid-layout measures with parseFloat(getComputedStyle(node).width), so a container
    // computed as `width: 100%` measures as 100 — a plausible-looking number that is not a
    // width. Believing it turns a 1440px desktop board into one stacked card.
    expect(usableWidth(100)).toBe(ASSUMED_WIDTH);
    expect(usableWidth(0)).toBe(ASSUMED_WIDTH);
    expect(usableWidth(Number.NaN)).toBe(ASSUMED_WIDTH);
    expect(cardOrientation(usableWidth(100))).toBe("vertical");
  });

  it("believes any real viewport, down to the narrowest phone", () => {
    expect(usableWidth(MIN_MEASURED_WIDTH)).toBe(MIN_MEASURED_WIDTH);
    expect(usableWidth(390)).toBe(390);
    expect(usableWidth(1440)).toBe(1440);
  });
});
