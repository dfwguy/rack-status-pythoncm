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
// tableSort.ts — one column-sort rule for the Scheduler tables.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// squeue and sinfo print whatever columns the site's format string asks for, so sorting cannot be
// keyed off column names. The comparator infers the type from the values instead, which is also
// what makes it correct where a plain string sort is actively wrong:
//
//   TIME     "9:59" must come before "14:31", and "1-02:03:04" (a day) after both
//   JOBID    "99" before "140212"; array ids like "140212_3" order by task within the job
//   NODES    "9" before "18"
//   NODELIST "ink50-dh01-h9" before "ink50-dh01-h10"
//
// Blank-ish cells always sort last, in both directions: they carry no information, and an
// operator sorting a column is hunting for a value, not for the gaps.
export type SortDir = "asc" | "desc";

/** Which column a table is ordered by, and which way. ``col: null`` is the feed's own order. */
export interface SortState {
  col: number | null;
  dir: SortDir;
}

/** Cells that mean "nothing here" and are pushed to the bottom whichever way the column sorts. */
const BLANKS = new Set(["", "-", "--", "n/a", "none", "(none)", "null", "(null)"]);

/** ``MM:SS``, ``HH:MM:SS`` or ``D-HH:MM:SS`` — Slurm's TIME/ELAPSED/TIMELIMIT format. */
const DURATION_RE = /^(?:(\d+)-)?(\d{1,3}):([0-5]\d)(?::([0-5]\d))?$/;
const NUMERIC_RE = /^[+-]?\d+(?:\.\d+)?$/;
const ISO_RE = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?/;

/** Seconds for a Slurm duration, else null. */
export function durationSeconds(raw: string): number | null {
  const m = DURATION_RE.exec(raw.trim());
  if (!m) return null;
  const [, d, a, b, c] = m;
  const days = d ? Number(d) : 0;
  // With three parts it is H:M:S; with two it is M:S. Slurm prints both.
  const [h, min, s] = c != null ? [Number(a), Number(b), Number(c)] : [0, Number(a), Number(b)];
  return ((days * 24 + h) * 60 + min) * 60 + s;
}

/** Compare mixed digit/letter runs so 9 precedes 10 and h9 precedes h10. */
function naturalCompare(a: string, b: string): number {
  const ax = a.match(/\d+|\D+/g) ?? [];
  const bx = b.match(/\d+|\D+/g) ?? [];
  for (let i = 0; i < Math.min(ax.length, bx.length); i++) {
    const x = ax[i];
    const y = bx[i];
    const xn = /^\d/.test(x);
    const yn = /^\d/.test(y);
    if (xn && yn) {
      const d = Number(x) - Number(y);
      if (d !== 0) return d;
    } else {
      const d = x.localeCompare(y, undefined, { sensitivity: "base" });
      if (d !== 0) return d;
    }
  }
  return ax.length - bx.length;
}

/**
 * Order two raw cells. Returns <0, 0 or >0 for ascending order; blanks are NOT handled here
 * (see {@link sortRows}, which keeps them at the bottom in both directions).
 */
export function compareCells(a: string, b: string): number {
  const x = a.trim();
  const y = b.trim();

  const dx = durationSeconds(x);
  const dy = durationSeconds(y);
  if (dx !== null && dy !== null) return dx - dy;

  if (NUMERIC_RE.test(x) && NUMERIC_RE.test(y)) return Number(x) - Number(y);

  if (ISO_RE.test(x) && ISO_RE.test(y)) {
    const tx = Date.parse(x);
    const ty = Date.parse(y);
    if (!Number.isNaN(tx) && !Number.isNaN(ty) && tx !== ty) return tx - ty;
  }

  return naturalCompare(x, y);
}

/**
 * The next sort state for a click on *col*: ascending, then descending, then back to the feed's
 * own order. That third step matters — squeue prints in priority order, which is information the
 * user should be able to get back without reloading.
 */
export function nextSortState(cur: SortState, col: number): SortState {
  if (cur.col !== col) return { col, dir: "asc" };
  if (cur.dir === "asc") return { col, dir: "desc" };
  return { col: null, dir: "asc" };
}

/**
 * A sorted copy of *rows*, or the original array when no column is selected.
 *
 * Stable: equal cells keep their incoming order, so sorting by a coarse column (ST, PARTITION)
 * leaves the scheduler's own ordering intact inside each group.
 */
export function sortRows<T>(
  rows: readonly T[],
  cell: (row: T, col: number) => string,
  sort: SortState,
): T[] {
  const { col, dir } = sort;
  if (col == null) return rows as T[];
  const sign = dir === "asc" ? 1 : -1;
  return rows
    .map((row, i) => ({ row, i, v: (cell(row, col) ?? "").trim() }))
    .sort((p, q) => {
      const pb = BLANKS.has(p.v.toLowerCase());
      const qb = BLANKS.has(q.v.toLowerCase());
      if (pb !== qb) return pb ? 1 : -1; // blanks last, both directions
      if (pb && qb) return p.i - q.i;
      const d = compareCells(p.v, q.v);
      return d !== 0 ? d * sign : p.i - q.i;
    })
    .map((e) => e.row);
}
