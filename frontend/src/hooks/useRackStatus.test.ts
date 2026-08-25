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
import { afterEach, describe, it, expect, vi, beforeEach } from "vitest";
import { DEFAULT_RACKS_POLL_MS, useRackStatus } from "./useRackStatus";

describe("useRackStatus", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("module exports useRackStatus function", async () => {
    const mod = await import("./useRackStatus");
    expect(typeof mod.useRackStatus).toBe("function");
    expect(mod.DEFAULT_RACKS_POLL_MS).toBe(30_000);
  });

  it("polls the Racks status endpoint every 30 seconds and stops on cleanup", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true, snapshot: { id: "one", state: "fresh" } }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const { unmount } = renderHook(() => useRackStatus("/logs"));
    await act(async () => { await Promise.resolve(); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(DEFAULT_RACKS_POLL_MS - 1); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(DEFAULT_RACKS_POLL_MS); });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("uses only the 3-second bootstrap retry while the PythonCM snapshot initializes", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ ok: true, snapshot: { id: "boot", state: "initializing" } }),
      })
      .mockResolvedValue({
        ok: true,
        json: async () => ({ ok: true, snapshot: { id: "ready", state: "fresh" } }),
      });
    vi.stubGlobal("fetch", fetchMock);
    const { unmount } = renderHook(() => useRackStatus("/logs"));
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(2_999); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    unmount();
  });
});
