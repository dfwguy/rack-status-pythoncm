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
// useSortedPagedRows.ts — sort a table by column, then page it.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Sorting and paging are composed here rather than left to each panel because the order of the
// two is not optional: sort first, page the sorted rows, and jump back to page 1 on a sort
// change. Sorting only the visible page would reorder 25 rows out of 300 and quietly lie, and
// staying on page 7 after a sort hides the very row the user sorted to find.
import { useCallback, useMemo, useState } from "react";
import { sortRows, nextSortState, type SortState } from "../lib/tableSort";
import { usePagedRows, type PagedRows } from "./usePagedRows";

export interface SortedPagedRows<T> extends PagedRows<T> {
  /** Column index currently sorted, or null for the feed's own order. */
  sortCol: number | null;
  sortDir: "asc" | "desc";
  /** Header click: asc -> desc -> back to the original order. */
  toggleSort: (col: number) => void;
}

/**
 * @param rows  every row, unsorted and unpaged
 * @param cell  the raw text of a column for one row; must be referentially stable (define it
 *              outside the component, or wrap it in useCallback)
 */
export function useSortedPagedRows<T>(
  rows: readonly T[],
  cell: (row: T, col: number) => string,
  initialPageSize?: number,
): SortedPagedRows<T> {
  const [sort, setSort] = useState<SortState>({ col: null, dir: "asc" });
  const sorted = useMemo(() => sortRows(rows, cell, sort), [rows, cell, sort]);
  const paged = usePagedRows(sorted, initialPageSize);

  const { setPage } = paged;
  const toggleSort = useCallback(
    (col: number) => {
      setSort((cur) => nextSortState(cur, col));
      setPage(1);
    },
    [setPage],
  );

  return { ...paged, sortCol: sort.col, sortDir: sort.dir, toggleSort };
}
