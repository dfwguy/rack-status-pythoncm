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
/**
 * Client-side pagination for a table whose row count scales with the cluster.
 *
 * Extracted because the alternative is the same six lines of page/pageSize/slice arithmetic in
 * a dozen components, each free to get the edge cases subtly wrong. The two that matter:
 *
 *   - The page must be CLAMPED, not just stored. Every one of these tables sits behind a live
 *     feed or a filter, so the row count shrinks under the user: type a filter while on page 7
 *     of 9 and a naive `slice` returns an empty table that looks like "no results". Clamping
 *     derives the effective page from the current total instead, so the view lands on the last
 *     real page and never renders blank.
 *   - `total` is the FILTERED length, so the pager reports what the user is looking at rather
 *     than what the server sent.
 *
 * Deliberately client-side: these rows already arrive in one payload (squeue output, a log
 * directory listing, a parser aggregate), so paging is a rendering concern. Nothing here should
 * be read as a reason to keep shipping unbounded payloads.
 */
import { useMemo, useState } from "react";

/** Options offered in the page-size selector, smallest first. */
export const PAGE_SIZE_OPTIONS = [10, 25, 50, 100] as const;

/** Rows per page when a caller does not care. 25 fills a screen without a scroll marathon. */
export const DEFAULT_PAGE_SIZE = 25;

export interface PagedRows<T> {
  /** The slice to render. */
  visible: T[];
  /** 1-based effective page, already clamped to the current row count. */
  page: number;
  setPage: (page: number) => void;
  pageSize: number;
  setPageSize: (size: number) => void;
  /** Number of rows being paged (post-filter), for the pager's range text. */
  total: number;
  /** True when the pager is worth rendering at all. */
  hasPages: boolean;
}

export function usePagedRows<T>(rows: readonly T[], initialPageSize = DEFAULT_PAGE_SIZE): PagedRows<T> {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(initialPageSize);

  const total = rows.length;
  // Clamp rather than reset: a caller that forgets to reset the page on filter change still
  // cannot end up on an empty page, and the user keeps their position when rows only grow.
  const maxPage = Math.max(1, Math.ceil(total / pageSize));
  const safePage = Math.min(Math.max(1, page), maxPage);

  const visible = useMemo(
    () => rows.slice((safePage - 1) * pageSize, (safePage - 1) * pageSize + pageSize) as T[],
    [rows, safePage, pageSize],
  );

  return {
    visible,
    page: safePage,
    setPage,
    pageSize,
    setPageSize: (size: number) => {
      setPageSize(size);
      // Growing the page size can strand the user past the new last page.
      setPage(1);
    },
    total,
    hasPages: total > pageSize,
  };
}
