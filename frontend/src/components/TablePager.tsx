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
 * The pager under a table, on Kaizen UI's Pagination.
 *
 * Renders NOTHING when everything fits on one page. That is what lets the same control go under
 * every table in the app without cluttering the small ones: a rack's 18 trays or a five-row
 * bring-up summary simply never show a pager, while the same component handles a 2000-row squeue.
 */
import { Pagination } from "@nvidia/foundations-react-core";
import { PAGE_SIZE_OPTIONS, type PagedRows } from "../hooks/usePagedRows";

export interface TablePagerProps<T> extends Pick<
  PagedRows<T>,
  "page" | "setPage" | "pageSize" | "setPageSize" | "total" | "hasPages"
> {
  /** What the rows are, for the range text and the accessible name ("jobs", "logs", "racks"). */
  noun: string;
  /** Page sizes to offer; defaults to 10/25/50/100. */
  pageSizeOptions?: readonly number[];
  /** Drop the page-size selector where horizontal room is tight (dialogs, dashboard widgets). */
  compact?: boolean;
}

export function TablePager<T>({
  page,
  setPage,
  pageSize,
  setPageSize,
  total,
  hasPages,
  noun,
  pageSizeOptions = PAGE_SIZE_OPTIONS,
  compact = false,
}: TablePagerProps<T>) {
  if (!hasPages) return null;
  return (
    <div style={{ marginTop: 12 }}>
      <Pagination
        displayControls
        totalItems={total}
        page={page}
        pageSize={pageSize}
        pageSizeOptions={compact ? undefined : [...pageSizeOptions]}
        onPageChange={setPage}
        onPageSizeChange={setPageSize}
        aria-label={`${noun} pagination`}
        rangeTextFormatFn={({
          firstItemIndex,
          lastItemIndex,
          totalItems,
        }: {
          firstItemIndex: number;
          lastItemIndex: number;
          totalItems: number;
        }) => `${firstItemIndex}–${lastItemIndex} of ${totalItems} ${noun}`}
      />
    </div>
  );
}
