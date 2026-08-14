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
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useBringupNode, clearBringupCache } from "./useBringupNode";
import type { BringupNodeSummary } from "../types/bringup";

const getBringupNode = vi.hoisted(() => vi.fn());
vi.mock("../services/bringupService", () => ({ getBringupNode }));

function summary(node: string): BringupNodeSummary {
  return { node, present: true, slurm_state: "idle", home: "/home/cmods/bringup", jobs: [] };
}

beforeEach(() => {
  clearBringupCache();
  getBringupNode.mockReset();
  getBringupNode.mockImplementation((n: string) => Promise.resolve(summary(n)));
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useBringupNode caching", () => {
  it("fetches once and exposes the summary + lastUpdated", async () => {
    const { result } = renderHook(() => useBringupNode("n1"));
    await waitFor(() => expect(result.current.data?.node).toBe("n1"));
    expect(getBringupNode).toHaveBeenCalledTimes(1);
    expect(result.current.lastUpdated).toBeTypeOf("number");
  });

  it("serves a second mount from cache without re-fetching, and with no loading flash", async () => {
    const first = renderHook(() => useBringupNode("n1"));
    await waitFor(() => expect(first.result.current.data).not.toBeNull());
    first.unmount();

    const second = renderHook(() => useBringupNode("n1"));
    // Cached data is available on the very first render — the point of the cache.
    expect(second.result.current.data?.node).toBe("n1");
    expect(second.result.current.loading).toBe(false);
    expect(getBringupNode).toHaveBeenCalledTimes(1);
  });

  it("de-duplicates concurrent requests for the same node", async () => {
    const a = renderHook(() => useBringupNode("n1"));
    const b = renderHook(() => useBringupNode("n1"));
    await waitFor(() => expect(a.result.current.data).not.toBeNull());
    await waitFor(() => expect(b.result.current.data).not.toBeNull());
    expect(getBringupNode).toHaveBeenCalledTimes(1);
  });

  it("keeps separate cache entries per node", async () => {
    const { result, rerender } = renderHook(({ n }) => useBringupNode(n), {
      initialProps: { n: "n1" },
    });
    await waitFor(() => expect(result.current.data?.node).toBe("n1"));
    rerender({ n: "n2" });
    await waitFor(() => expect(result.current.data?.node).toBe("n2"));
    expect(getBringupNode).toHaveBeenCalledTimes(2);
    // Back to n1 -> served from cache, still only two calls total.
    rerender({ n: "n1" });
    expect(result.current.data?.node).toBe("n1");
    expect(getBringupNode).toHaveBeenCalledTimes(2);
  });

  it("refresh() bypasses the cache", async () => {
    const { result } = renderHook(() => useBringupNode("n1"));
    await waitFor(() => expect(result.current.data).not.toBeNull());
    await act(async () => {
      result.current.refresh();
    });
    await waitFor(() => expect(getBringupNode).toHaveBeenCalledTimes(2));
  });

  it("surfaces an error without throwing", async () => {
    getBringupNode.mockRejectedValueOnce(new Error("boom"));
    const { result } = renderHook(() => useBringupNode("n1"));
    await waitFor(() => expect(result.current.error).toBe("boom"));
    expect(result.current.data).toBeNull();
  });

  it("returns an inert result when no node is selected", () => {
    const { result } = renderHook(() => useBringupNode(null));
    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(false);
    expect(getBringupNode).not.toHaveBeenCalled();
  });
});

describe("useBringupNode auto-refresh", () => {
  it("re-fetches on the interval when enabled", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useBringupNode("n1", 60_000));
    await vi.waitFor(() => expect(result.current.data).not.toBeNull());
    expect(getBringupNode).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(getBringupNode).toHaveBeenCalledTimes(2);
  });

  it("does not poll when the interval is off (0)", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useBringupNode("n1", 0));
    await vi.waitFor(() => expect(result.current.data).not.toBeNull());

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30 * 60_000);
    });
    expect(getBringupNode).toHaveBeenCalledTimes(1);
  });

  it("skips a tick while the tab is hidden", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useBringupNode("n1", 60_000));
    await vi.waitFor(() => expect(result.current.data).not.toBeNull());

    const spy = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(getBringupNode).toHaveBeenCalledTimes(1); // no extra call while hidden
    spy.mockRestore();
  });
});
