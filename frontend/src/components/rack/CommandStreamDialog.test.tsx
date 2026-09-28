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
 * Streamed commands (sysinfo, NVSwitch audit, log tail) go through `apiFetch`: an expired access
 * token is renewed before the body is read, and the operator's attribution headers are sent.
 */
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { onSessionLost } from "../../lib/apiFetch";
import { CommandStreamDialog } from "./CommandStreamDialog";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  onSessionLost(null);
  vi.restoreAllMocks();
});

const json = (body: unknown, status: number) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("CommandStreamDialog", () => {
  it("renews an expired session, then streams the command output", async () => {
    const calls: { url: string; clientId: string | null }[] = [];
    let served = 0;
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, clientId: new Headers(init?.headers).get("X-Rack-Client-Id") });
      if (url.includes("/api/auth/refresh")) return json({ ok: true }, 200);
      served += 1;
      return served === 1
        ? json({ error: "authentication required" }, 401)
        : new Response("== sysinfo g01 ==\nok\n", { status: 200 });
    }) as typeof fetch;

    render(
      <CommandStreamDialog
        open
        onClose={() => {}}
        title="sysinfo"
        endpoint="/api/run-sysinfo-stream"
        body={{ rack: "g01" }}
      />,
    );

    expect(await screen.findByText(/== sysinfo g01 ==/)).toBeInTheDocument();
    expect(calls.map((c) => c.url)).toEqual([
      "/api/run-sysinfo-stream",
      "/api/auth/refresh",
      "/api/run-sysinfo-stream",
    ]);
    // Sent by apiFetch on every request; a raw fetch left rack-lock tracking without it.
    expect(calls[0].clientId).toMatch(/^spa-/);
  });

  it("shows the server's error text instead of streaming", async () => {
    globalThis.fetch = (async () => json({ error: "rack g99 not found" }, 404)) as typeof fetch;

    render(
      <CommandStreamDialog
        open
        onClose={() => {}}
        title="audit"
        endpoint="/api/run-nvswitch-audit-stream"
        body={{ rack: "g99" }}
      />,
    );

    expect(await screen.findByText("rack g99 not found")).toBeInTheDocument();
  });
});
