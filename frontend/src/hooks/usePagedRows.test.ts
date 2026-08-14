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
import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DEFAULT_PAGE_SIZE, usePagedRows } from "./usePagedRows";

const rows = (n: number) => Array.from({ length: n }, (_, i) => i + 1);

describe("usePagedRows", () => {
  it("slices to the first page and reports the total", () => {
    const { result } = renderHook(() => usePagedRows(rows(120), 25));
    expect(result.current.visible).toHaveLength(25);
    expect(result.current.visible[0]).toBe(1);
    expect(result.current.total).toBe(120);
    expect(result.current.page).toBe(1);
  });

  it("hides the pager when everything fits", () => {
    const { result } = renderHook(() => usePagedRows(rows(10), 25));
    expect(result.current.hasPages).toBe(false);
    expect(result.current.visible).toHaveLength(10);
  });

  it("serves a short final page", () => {
    const { result } = renderHook(() => usePagedRows(rows(30), 25));
    act(() => result.current.setPage(2));
    expect(result.current.visible).toEqual([26, 27, 28, 29, 30]);
  });

  it("clamps to the last real page when the rows shrink under the user", () => {
    /**
     * The bug this exists to prevent: filtering while on a high page used to leave the table
     * empty, which reads as "no results" rather than "you are past the end".
     */
    const { result, rerender } = renderHook(({ n }) => usePagedRows(rows(n), 10), {
      initialProps: { n: 100 },
    });
    act(() => result.current.setPage(9));
    expect(result.current.visible[0]).toBe(81);

    rerender({ n: 12 });
    expect(result.current.page).toBe(2);
    expect(result.current.visible).toEqual([11, 12]);
    expect(result.current.visible.length).toBeGreaterThan(0);
  });

  it("never yields an empty page for a non-empty list", () => {
    const { result, rerender } = renderHook(({ n }) => usePagedRows(rows(n), 10), {
      initialProps: { n: 100 },
    });
    act(() => result.current.setPage(10));
    for (const n of [55, 21, 7, 1]) {
      rerender({ n });
      expect(result.current.visible.length).toBeGreaterThan(0);
    }
  });

  it("handles an empty list without dividing by zero", () => {
    const { result } = renderHook(() => usePagedRows([], 25));
    expect(result.current.visible).toEqual([]);
    expect(result.current.page).toBe(1);
    expect(result.current.hasPages).toBe(false);
  });

  it("returns to page 1 when the page size changes, so the view cannot strand", () => {
    const { result } = renderHook(() => usePagedRows(rows(100), 10));
    act(() => result.current.setPage(9));
    act(() => result.current.setPageSize(50));
    expect(result.current.page).toBe(1);
    expect(result.current.visible).toHaveLength(50);
  });

  it("defaults to a sensible page size", () => {
    const { result } = renderHook(() => usePagedRows(rows(500)));
    expect(result.current.pageSize).toBe(DEFAULT_PAGE_SIZE);
  });

  it("keeps the user's position when rows are only appended", () => {
    /** A live feed (squeue) grows between polls; that must not yank the page back to 1. */
    const { result, rerender } = renderHook(({ n }) => usePagedRows(rows(n), 10), {
      initialProps: { n: 50 },
    });
    act(() => result.current.setPage(3));
    rerender({ n: 80 });
    expect(result.current.page).toBe(3);
    expect(result.current.visible[0]).toBe(21);
  });
});
