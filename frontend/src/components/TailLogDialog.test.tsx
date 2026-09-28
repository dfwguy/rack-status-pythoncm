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
 * The live tail streams through `apiFetch`, so an expired access token is renewed and the stream
 * still opens. It used a raw `fetch` and, on a 401, navigated to `/login` — losing the hash route
 * and bypassing the app's sign-out handler.
 */
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { onSessionLost } from "../lib/apiFetch";
import { TailLogDialog } from "./TailLogDialog";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  onSessionLost(null);
  vi.restoreAllMocks();
});

const json = (body: unknown, status: number) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("TailLogDialog", () => {
  it("renews an expired session and then reads the stream", async () => {
    const calls: string[] = [];
    let served = 0;
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.includes("/api/auth/refresh")) return json({ ok: true }, 200);
      served += 1;
      return served === 1
        ? json({ error: "authentication required" }, 401)
        : new Response("line one\nline two\n", { status: 200 });
    }) as typeof fetch;
    const hrefBefore = window.location.href;

    render(<TailLogDialog open onClose={() => {}} title="tail g01" payload={{ rack: "g01" }} />);

    expect(await screen.findByText(/line one\s+line two/)).toBeInTheDocument();
    expect(calls).toEqual(["/api/tail-log-stream", "/api/auth/refresh", "/api/tail-log-stream"]);
    expect(window.location.href).toBe(hrefBefore);
  });

  it("signs out through apiFetch and stays on the page when the session is gone", async () => {
    const lost = vi.fn();
    onSessionLost(lost);
    globalThis.fetch = (async () => json({ error: "authentication required" }, 401)) as typeof fetch;
    const hrefBefore = window.location.href;

    render(
      <TailLogDialog
        open
        onClose={() => {}}
        title="job stdout"
        payload={{ path: "/logs/slurm-1.out" }}
        streamEndpoint="/api/tail-job-stdout-stream"
      />,
    );

    expect(await screen.findByText(/session has expired/)).toBeInTheDocument();
    expect(lost).toHaveBeenCalledTimes(1);
    expect(window.location.href).toBe(hrefBefore);
  });
});
