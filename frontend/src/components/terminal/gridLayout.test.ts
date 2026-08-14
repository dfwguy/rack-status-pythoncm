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
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// The placement rules are the part of the grid with actual logic in them, and the part where a
// subtle error looks like a UI glitch rather than a bug: a pane pushed onto its own row, or an
// operator's hand-built arrangement silently re-tiled when they add a shell.
import { describe, it, expect } from "vitest";
import {
  GRID_COLS,
  MIN_PANE_COLS,
  MIN_PANE_ROWS,
  appendToLayout,
  autoLayout,
  removeFromLayout,
  sameLayout,
} from "./gridLayout";

const ids = (n: number) => Array.from({ length: n }, (_, i) => `p${i + 1}`);

/** Panes must never overlap; two occupying the same cell renders one on top of the other. */
function overlaps(layout: { x: number; y: number; w: number; h: number }[]): boolean {
  for (let a = 0; a < layout.length; a++) {
    for (let b = a + 1; b < layout.length; b++) {
      const p = layout[a];
      const q = layout[b];
      const apart =
        p.x + p.w <= q.x || q.x + q.w <= p.x || p.y + p.h <= q.y || q.y + q.h <= p.y;
      if (!apart) return true;
    }
  }
  return false;
}

describe("autoLayout — the specified progression", () => {
  it("gives a lone terminal the whole width", () => {
    const [only] = autoLayout(ids(1));
    expect(only.w).toBe(GRID_COLS);
    expect(only.x).toBe(0);
    expect(only.y).toBe(0);
  });

  it("puts two side by side on one row, not stacked", () => {
    const l = autoLayout(ids(2));
    expect(l.map((p) => p.y)).toEqual([0, 0]);
    expect(l[0].w + l[1].w).toBe(GRID_COLS);
    expect(l[1].x).toBe(GRID_COLS / 2);
  });

  it("tiles three and four as 2x2", () => {
    for (const n of [3, 4]) {
      const l = autoLayout(ids(n));
      expect(new Set(l.map((p) => p.x)).size).toBe(2);
      expect(new Set(l.map((p) => p.y)).size).toBe(2);
    }
  });

  it("keeps growing rows beyond four without overlapping", () => {
    for (const n of [5, 6, 7, 8]) {
      const l = autoLayout(ids(n));
      expect(l).toHaveLength(n);
      expect(overlaps(l)).toBe(false);
    }
  });

  it("lets any pane be widened to the whole row", () => {
    // Dragging a second pane below the first leaves the top row half empty; widening the pane
    // across it is the obvious fix, and a width cap at the placement default would forbid that.
    for (const p of autoLayout(ids(4))) {
      expect(p.maxW).toBe(GRID_COLS);
    }
    expect(appendToLayout(autoLayout(ids(1)), "p2")[1].maxW).toBe(GRID_COLS);
  });

  it("starts panes tall enough to be worth reading", () => {
    // 14 rows is ~524px by react-grid-layout's sizing, close to 30 lines of terminal.
    for (const p of autoLayout(ids(2))) {
      expect(p.h).toBeGreaterThanOrEqual(12);
    }
  });

  it("never places a pane too small to be a usable shell", () => {
    for (const p of autoLayout(ids(6))) {
      expect(p.w).toBeGreaterThanOrEqual(MIN_PANE_COLS);
      expect(p.h).toBeGreaterThanOrEqual(MIN_PANE_ROWS);
      expect(p.minW).toBe(MIN_PANE_COLS);
      expect(p.minH).toBe(MIN_PANE_ROWS);
    }
  });

  it("is empty for no panes rather than inventing one", () => {
    expect(autoLayout([])).toEqual([]);
  });
});

describe("appendToLayout — adding without wrecking the arrangement", () => {
  it("halves a full-width lone pane so the second lands beside it", () => {
    const one = autoLayout(ids(1));
    const two = appendToLayout(one, "p2");
    expect(two).toHaveLength(2);
    expect(two.map((p) => p.y)).toEqual([0, 0]);
    expect(two[0].w).toBe(GRID_COLS / 2);
    expect(two[1].x).toBe(GRID_COLS / 2);
  });

  it("fills the free half of the last row before starting a new one", () => {
    const three = appendToLayout(autoLayout(ids(2)), "p3");
    const bottom = three[2];
    expect(bottom.x).toBe(0);
    expect(bottom.y).toBeGreaterThan(0);

    const four = appendToLayout(three, "p4");
    expect(four[3].y).toBe(bottom.y); // same row, beside p3
    expect(four[3].x).toBe(GRID_COLS / 2);
  });

  it("leaves the existing panes exactly where the operator put them", () => {
    // A deliberately hand-built arrangement: one tall pane left, one short pane right.
    const arranged = [
      { i: "p1", x: 0, y: 0, w: 8, h: 20 },
      { i: "p2", x: 8, y: 0, w: 4, h: 6 },
    ];
    const after = appendToLayout(arranged, "p3");
    expect(after.slice(0, 2)).toEqual(arranged);
    expect(overlaps(after)).toBe(false);
  });

  it("does not overlap when the last row is already full", () => {
    let layout = autoLayout(ids(2));
    for (const id of ["p3", "p4", "p5", "p6"]) layout = appendToLayout(layout, id);
    expect(layout).toHaveLength(6);
    expect(overlaps(layout)).toBe(false);
  });

  it("treats an empty layout as the first pane", () => {
    const first = appendToLayout([], "p1");
    expect(first).toHaveLength(1);
    expect(first[0].w).toBe(GRID_COLS);
  });
});

describe("sameLayout — the guard against echoing a layout back", () => {
  it("treats an identical arrangement as unchanged", () => {
    expect(sameLayout(autoLayout(ids(3)), autoLayout(ids(3)))).toBe(true);
  });

  it("ignores key order, since the grid may hand items back in any order", () => {
    const a = autoLayout(ids(3));
    expect(sameLayout(a, [...a].reverse())).toBe(true);
  });

  it("notices a moved pane", () => {
    const a = autoLayout(ids(2));
    const moved = a.map((p, i) => (i === 0 ? { ...p, y: p.y + 4 } : p));
    expect(sameLayout(a, moved)).toBe(false);
  });

  it("notices a resized pane", () => {
    const a = autoLayout(ids(2));
    const resized = a.map((p, i) => (i === 1 ? { ...p, h: p.h + 3 } : p));
    expect(sameLayout(a, resized)).toBe(false);
  });

  it("notices an added or removed pane", () => {
    expect(sameLayout(autoLayout(ids(2)), autoLayout(ids(3)))).toBe(false);
  });

  it("notices a renamed pane even when the geometry matches", () => {
    const a = autoLayout(ids(2));
    const renamed = a.map((p, i) => (i === 0 ? { ...p, i: "other" } : p));
    expect(sameLayout(a, renamed)).toBe(false);
  });

  it("considers two empty layouts the same", () => {
    expect(sameLayout([], [])).toBe(true);
  });
});

describe("removeFromLayout", () => {
  it("drops only the pane asked for", () => {
    const after = removeFromLayout(autoLayout(ids(3)), "p2");
    expect(after.map((p) => p.i)).toEqual(["p1", "p3"]);
  });

  it("restores full width when one pane is left, so no half-empty row survives a reload", () => {
    const after = removeFromLayout(autoLayout(ids(2)), "p2");
    expect(after).toHaveLength(1);
    expect(after[0].w).toBe(GRID_COLS);
    expect(after[0].x).toBe(0);
  });

  it("is a no-op for an id that is not there", () => {
    const before = autoLayout(ids(3));
    expect(removeFromLayout(before, "nope")).toEqual(before);
  });

  it("empties cleanly when the last pane closes", () => {
    expect(removeFromLayout(autoLayout(ids(1)), "p1")).toEqual([]);
  });
});
