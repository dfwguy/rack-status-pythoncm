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
// The cases that make a plain string sort wrong on a real squeue table. Values are taken from
// the Nscale TX Scheduler view (jobs 140186-140212 on ink50-dh01).
import { describe, expect, it } from "vitest";
import { compareCells, durationSeconds, nextSortState, sortRows, type SortState } from "./tableSort";

const asc: SortState = { col: 0, dir: "asc" };
const desc: SortState = { col: 0, dir: "desc" };
const cell = (r: string[], c: number) => r[c] ?? "";

/** Sort a single column of raw cells and return it as a flat list. */
const order = (values: string[], sort = asc) =>
  sortRows(values.map((v) => [v]), cell, sort).map((r) => r[0]);

describe("durationSeconds", () => {
  it("reads both of Slurm's shapes", () => {
    expect(durationSeconds("0:00")).toBe(0);
    expect(durationSeconds("2:59")).toBe(179);
    expect(durationSeconds("14:31")).toBe(871);
    expect(durationSeconds("00:00:42")).toBe(42);
    expect(durationSeconds("1-02:03:04")).toBe(93784);
  });

  it("declines anything that is not a duration", () => {
    expect(durationSeconds("140212")).toBeNull();
    expect(durationSeconds("defq")).toBeNull();
    expect(durationSeconds("")).toBeNull();
  });
});

describe("compareCells", () => {
  it("orders TIME by elapsed, not by text", () => {
    // the bug this exists to prevent: "14:31" sorts before "9:59" as a string
    expect(compareCells("9:59", "14:31")).toBeLessThan(0);
    expect(compareCells("14:31", "1-02:03:04")).toBeLessThan(0);
  });

  it("orders numeric columns numerically", () => {
    expect(compareCells("9", "18")).toBeLessThan(0);
    expect(compareCells("99", "140212")).toBeLessThan(0);
  });

  it("orders node names by their number, not digit by digit", () => {
    expect(compareCells("ink50-dh01-h9", "ink50-dh01-h10")).toBeLessThan(0);
    expect(compareCells("ink50-dh01-j05-14", "ink50-dh01-j05-31")).toBeLessThan(0);
  });

  it("keeps array tasks with their job", () => {
    expect(compareCells("140212_3", "140212_11")).toBeLessThan(0);
    expect(compareCells("140212_9", "140213_1")).toBeLessThan(0);
  });

  it("orders end timestamps chronologically", () => {
    expect(compareCells("2026-08-10T13:53:28", "2026-08-10T13:53:31")).toBeLessThan(0);
  });
});

describe("sortRows", () => {
  it("sorts job ids ascending and descending", () => {
    const ids = ["140212", "99", "140186", "140112"];
    expect(order(ids)).toEqual(["99", "140112", "140186", "140212"]);
    expect(order(ids, desc)).toEqual(["140212", "140186", "140112", "99"]);
  });

  it("sorts TIME so the longest-running job is findable", () => {
    const times = ["0:00", "14:31", "2:59", "3:13"];
    expect(order(times)).toEqual(["0:00", "2:59", "3:13", "14:31"]);
    expect(order(times, desc)[0]).toBe("14:31");
  });

  it("keeps blank cells at the bottom whichever way the column sorts", () => {
    const withGaps = ["b", "", "a", "-"];
    expect(order(withGaps).slice(0, 2)).toEqual(["a", "b"]);
    expect(order(withGaps, desc).slice(0, 2)).toEqual(["b", "a"]);
    // the blanks are last in both cases, not floated to the top by the flip
    expect(order(withGaps).slice(2).every((v) => v === "" || v === "-")).toBe(true);
    expect(order(withGaps, desc).slice(2).every((v) => v === "" || v === "-")).toBe(true);
  });

  it("is stable, so a coarse column keeps the scheduler's order inside each group", () => {
    const rows = [
      ["PD", "140212"],
      ["R", "140382"],
      ["PD", "140186"],
      ["R", "140391"],
    ];
    const byState = sortRows(rows, cell, { col: 0, dir: "asc" });
    expect(byState.map((r) => r[1])).toEqual(["140212", "140186", "140382", "140391"]);
  });

  it("returns the original order when no column is selected", () => {
    const rows = [["b"], ["a"]];
    expect(sortRows(rows, cell, { col: null, dir: "asc" })).toBe(rows);
  });

  it("does not mutate the caller's array", () => {
    const rows = [["b"], ["a"]];
    sortRows(rows, cell, asc);
    expect(rows.map((r) => r[0])).toEqual(["b", "a"]);
  });
});

describe("nextSortState", () => {
  it("cycles asc -> desc -> the feed's own order", () => {
    let s: SortState = { col: null, dir: "asc" };
    s = nextSortState(s, 2);
    expect(s).toEqual({ col: 2, dir: "asc" });
    s = nextSortState(s, 2);
    expect(s).toEqual({ col: 2, dir: "desc" });
    // a third click must restore squeue's own (priority) order
    s = nextSortState(s, 2);
    expect(s).toEqual({ col: null, dir: "asc" });
  });

  it("starts a different column ascending, whatever the previous direction", () => {
    expect(nextSortState({ col: 2, dir: "desc" }, 5)).toEqual({ col: 5, dir: "asc" });
  });
});
