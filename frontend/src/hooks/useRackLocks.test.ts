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
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";

describe("useRackLocks", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    localStorage.clear();
    localStorage.setItem("nvis-rack-operator", "alice");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  const okResponse = (body: unknown): Response =>
    ({
      ok: true,
      status: 200,
      json: async () => body,
    }) as unknown as Response;

  it("module exports useRackLocks function", async () => {
    const mod = await import("./useRackLocks");
    expect(typeof mod.useRackLocks).toBe("function");
  });

  it("loads the initial snapshot and exposes locks/me", async () => {
    fetchMock.mockResolvedValue(
      okResponse({
        ok: true,
        now: "2026-05-18T12:00:00Z",
        me: "alice",
        locks: {
          "p5-r01": {
            operator: "bob",
            kind: "claim",
            since: "2026-05-18T11:59:00Z",
            expires_at: "2026-05-18T12:29:00Z",
          },
        },
      }),
    );
    const { useRackLocks } = await import("./useRackLocks");
    const { result } = renderHook(() => useRackLocks(60_000));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.me).toBe("alice");
    expect(result.current.locks["p5-r01"]?.operator).toBe("bob");
  });

  it("isLockedByOther returns null for the caller's own locks", async () => {
    fetchMock.mockResolvedValue(
      okResponse({
        ok: true,
        now: "2026-05-18T12:00:00Z",
        me: "alice",
        locks: {
          "p5-r01": { operator: "alice", kind: "claim", since: "2026-05-18T11:59:00Z" },
          "p5-r02": { operator: "bob", kind: "active_job", since: "2026-05-18T11:50:00Z" },
        },
      }),
    );
    const { useRackLocks } = await import("./useRackLocks");
    const { result } = renderHook(() => useRackLocks(60_000));
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.isLockedByOther("p5-r01")).toBeNull();
    expect(result.current.isLockedByOther("p5-r02")?.operator).toBe("bob");
    expect(result.current.isLockedByOther("p9-r99")).toBeNull();
  });

  it("forwards the X-Rack-Operator header on the GET request", async () => {
    fetchMock.mockResolvedValue(
      okResponse({ ok: true, now: "", me: "alice", locks: {} }),
    );
    const { useRackLocks } = await import("./useRackLocks");
    renderHook(() => useRackLocks(60_000));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const headers = init.headers as Headers;
    expect(headers.get("X-Rack-Operator")).toBe("alice");
    expect(headers.get("X-Rack-Client-Id")).toBeTruthy();
  });

  it("refresh() re-fetches on demand", async () => {
    fetchMock.mockResolvedValue(
      okResponse({ ok: true, now: "", me: "alice", locks: {} }),
    );
    const { useRackLocks } = await import("./useRackLocks");
    const { result } = renderHook(() => useRackLocks(60_000));
    await waitFor(() => expect(result.current.loading).toBe(false));
    const initialCalls = fetchMock.mock.calls.length;

    await act(async () => {
      await result.current.refresh();
    });
    expect(fetchMock.mock.calls.length).toBeGreaterThan(initialCalls);
  });
});
