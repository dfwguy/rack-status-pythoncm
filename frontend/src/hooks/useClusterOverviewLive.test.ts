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
import { renderHook, act, waitFor } from "@testing-library/react";
import type {
  ClusterOverviewLiveResponse,
  ClusterSqueueBlock,
  LiveProbeSection,
  LiveProbeState,
} from "../types/api";

describe("useClusterOverviewLive", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  const okResponse = (body: unknown): Response =>
    ({
      ok: true,
      status: 200,
      json: async () => body,
    }) as unknown as Response;

  const errResponse = (status: number, body: unknown): Response =>
    ({
      ok: false,
      status,
      json: async () => body,
    }) as unknown as Response;

  const section = <T,>(state: LiveProbeState, value: T | null = null): LiveProbeSection<T> => ({
    state,
    value,
    last_attempt_at: null,
    last_success_at: null,
    age_seconds: null,
    last_error: null,
  });

  const liveBody = (
    overrides: Partial<ClusterOverviewLiveResponse> = {},
  ): ClusterOverviewLiveResponse => ({
    ok: true,
    cluster_id: "cluster-a",
    reachable: true,
    unreachable_detail: "",
    sections: {
      bcm: section<string>("fresh", "BCM 11.0"),
      slurm: section<string>("fresh", "slurm 25.05"),
      queue: section<ClusterSqueueBlock>("fresh", { ok: true, stdout: "JOBID\n1" }),
    },
    ...overrides,
  });

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    localStorage.clear();
    localStorage.setItem("nvis-rack-client-id", "test-client");
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("exports useClusterOverviewLive", async () => {
    const mod = await import("./useClusterOverviewLive");
    expect(typeof mod.useClusterOverviewLive).toBe("function");
  });

  it("starts loading and fetches /api/cluster-overview/live with no-store and client_id", async () => {
    let resolveFetch: (value: Response) => void = () => undefined;
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );

    const { useClusterOverviewLive } = await import("./useClusterOverviewLive");
    const { result } = renderHook(() => useClusterOverviewLive("/logs"));

    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();
    expect(result.current.error).toBeNull();

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/cluster-overview/live?");
    expect(url).toContain("client_id=test-client");
    expect(url).toContain("log_dir=%2Flogs");
    expect(url).not.toContain("refresh=");
    expect(init.cache).toBe("no-store");

    await act(async () => {
      resolveFetch(okResponse(liveBody()));
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data?.sections.bcm.value).toBe("BCM 11.0");
    expect(result.current.error).toBeNull();
  });

  it("preserves last data when a later request fails", async () => {
    fetchMock
      .mockResolvedValueOnce(okResponse(liveBody()))
      .mockResolvedValueOnce(errResponse(502, { ok: false, error: "probe gateway down" }));

    const { useClusterOverviewLive } = await import("./useClusterOverviewLive");
    const { result } = renderHook(() => useClusterOverviewLive(""));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data?.sections.bcm.value).toBe("BCM 11.0");

    await act(async () => {
      await result.current.retry();
    });

    await waitFor(() => expect(result.current.error).toBeTruthy());
    expect(result.current.data?.sections.bcm.value).toBe("BCM 11.0");
    expect(result.current.error).toMatch(/probe gateway down|HTTP 502/);
  });

  it("retry() adds refresh=1 to the live URL", async () => {
    fetchMock.mockResolvedValue(okResponse(liveBody()));

    const { useClusterOverviewLive } = await import("./useClusterOverviewLive");
    const { result } = renderHook(() => useClusterOverviewLive(""));

    await waitFor(() => expect(result.current.loading).toBe(false));
    const before = fetchMock.mock.calls.length;

    await act(async () => {
      await result.current.retry();
    });

    expect(fetchMock.mock.calls.length).toBeGreaterThan(before);
    const retryUrl = fetchMock.mock.calls.at(-1)?.[0] as string;
    expect(retryUrl).toContain("refresh=1");
  });

  it("polls every 3s while any section is initializing, else every 30s", async () => {
    vi.useFakeTimers();

    const initializing = liveBody({
      sections: {
        bcm: section<string>("initializing"),
        slurm: section<string>("fresh", "slurm 25.05"),
        queue: section<ClusterSqueueBlock>("fresh", { ok: true, stdout: "" }),
      },
    });
    const fresh = liveBody();

    fetchMock
      .mockResolvedValueOnce(okResponse(initializing))
      .mockResolvedValueOnce(okResponse(fresh))
      .mockResolvedValue(okResponse(fresh));

    const { useClusterOverviewLive } = await import("./useClusterOverviewLive");
    const { result } = renderHook(() => useClusterOverviewLive(""));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.data?.sections.bcm.state).toBe("initializing");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_999);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.current.data?.sections.bcm.state).toBe("fresh");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(29_999);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("clears the poll timer on unmount", async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(okResponse(liveBody()));

    const { useClusterOverviewLive } = await import("./useClusterOverviewLive");
    const { unmount } = renderHook(() => useClusterOverviewLive(""));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    unmount();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps 3s poll cadence after a transient error while sections are still initializing", async () => {
    vi.useFakeTimers();

    const initializing = liveBody({
      sections: {
        bcm: section<string>("initializing"),
        slurm: section<string>("fresh", "slurm 25.05"),
        queue: section<ClusterSqueueBlock>("fresh", { ok: true, stdout: "" }),
      },
    });
    const stillInitializing = liveBody({
      sections: {
        bcm: section<string>("initializing"),
        slurm: section<string>("fresh", "slurm 25.05"),
        queue: section<ClusterSqueueBlock>("fresh", { ok: true, stdout: "later" }),
      },
    });

    fetchMock
      .mockResolvedValueOnce(okResponse(initializing))
      .mockResolvedValueOnce(errResponse(502, { ok: false, error: "transient blip" }))
      .mockResolvedValueOnce(okResponse(stillInitializing));

    const { useClusterOverviewLive } = await import("./useClusterOverviewLive");
    const { result } = renderHook(() => useClusterOverviewLive(""));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current.data?.sections.bcm.state).toBe("initializing");
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Next poll at 3s fails transiently but must preserve initializing data
    // and stay on the fast cadence (not drop to 30s).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(result.current.data?.sections.bcm.state).toBe("initializing");
    expect(result.current.error).toMatch(/transient blip|HTTP 502/);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_999);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(result.current.error).toBeNull();
    expect(result.current.data?.sections.queue.value?.stdout).toBe("later");
  });

  it("ignores stale responses when a newer request (logDir change or overlap) wins", async () => {
    type Gate = { resolve: (value: Response) => void };
    const gates: Gate[] = [];
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          gates.push({ resolve });
        }),
    );

    const { useClusterOverviewLive } = await import("./useClusterOverviewLive");
    const { result, rerender, unmount } = renderHook(
      ({ logDir }: { logDir: string }) => useClusterOverviewLive(logDir),
      { initialProps: { logDir: "/old" } },
    );

    await waitFor(() => expect(gates.length).toBe(1));
    expect((fetchMock.mock.calls[0][0] as string)).toContain("log_dir=%2Fold");

    rerender({ logDir: "/new" });
    await waitFor(() => expect(gates.length).toBe(2));
    expect((fetchMock.mock.calls[1][0] as string)).toContain("log_dir=%2Fnew");

    // Older /old response resolves after /new — must not overwrite latest.
    await act(async () => {
      gates[1].resolve(okResponse(liveBody({ cluster_id: "from-new" })));
      gates[0].resolve(okResponse(liveBody({ cluster_id: "from-old" })));
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data?.cluster_id).toBe("from-new");
    expect(result.current.error).toBeNull();

    // Overlap: force-refresh starts first, then a newer logDir fetch starts.
    // The late refresh response must not overwrite the newer request.
    const refreshGate: Gate = { resolve: () => undefined };
    const newerGate: Gate = { resolve: () => undefined };
    fetchMock
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            refreshGate.resolve = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            newerGate.resolve = resolve;
          }),
      );

    await act(async () => {
      void result.current.retry();
    });
    await waitFor(() => expect(fetchMock.mock.calls.at(-1)?.[0] as string).toContain("refresh=1"));

    rerender({ logDir: "/new2" });
    await waitFor(() => expect(fetchMock.mock.calls.at(-1)?.[0] as string).toContain("log_dir=%2Fnew2"));
    expect((fetchMock.mock.calls.at(-1)?.[0] as string)).not.toContain("refresh=");

    await act(async () => {
      newerGate.resolve(okResponse(liveBody({ cluster_id: "from-newer" })));
      refreshGate.resolve(okResponse(liveBody({ cluster_id: "from-stale-refresh" })));
    });

    await waitFor(() => expect(result.current.data?.cluster_id).toBe("from-newer"));
    expect(result.current.data?.cluster_id).not.toBe("from-stale-refresh");
    expect(result.current.error).toBeNull();

    // In-flight resolve after unmount must not apply state.
    const afterUnmount: Gate = { resolve: () => undefined };
    fetchMock.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          afterUnmount.resolve = resolve;
        }),
    );
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    await act(async () => {
      void result.current.retry();
    });
    await waitFor(() =>
      expect(fetchMock.mock.calls.at(-1)?.[0] as string).toContain("refresh=1"),
    );
    unmount();
    await act(async () => {
      afterUnmount.resolve(okResponse(liveBody({ cluster_id: "after-unmount" })));
    });
    expect(consoleError).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });
});
