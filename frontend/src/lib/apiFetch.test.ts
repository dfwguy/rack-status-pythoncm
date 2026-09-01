/* SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 */

/**
 * Session renewal on 401 — the fix for a real, reported failure.
 *
 * Access tokens last 15 minutes by design: FR-6 permits a *withdrawn* grant to lag by at most that
 * long, so the TTL is the bound. `/api/auth/refresh` existed from the start and **nothing called
 * it**, so after fifteen minutes every request returned "authentication required" while the page
 * still looked signed in — the mount-time check had passed long before. That is what was reported:
 * a Users page that listed users and then refused to create one.
 *
 * Three properties, each guarding a different way this goes wrong:
 *
 * - a refresh is attempted **once** and the original request retried;
 * - concurrent 401s share **one** refresh, because a dashboard fires many requests at once and
 *   per-response refreshing would send a burst that races itself;
 * - the auth routes are **excluded**, or a failing refresh refreshes forever.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiFetch } from "./apiFetch";

const originalFetch = globalThis.fetch;

beforeEach(() => {
  vi.resetAllMocks();
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

describe("an expired session is renewed once and the request retried", () => {
  it("retries after a successful refresh", async () => {
    const calls: string[] = [];
    let served = 0;
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.includes("/api/auth/refresh")) return json({ ok: true });
      served += 1;
      return served === 1 ? json({ error: "authentication required" }, 401) : json({ ok: true });
    }) as typeof fetch;

    const resp = await apiFetch("/api/identity/users");

    expect(resp.status).toBe(200);
    expect(calls).toEqual([
      "/api/identity/users",
      "/api/auth/refresh",
      "/api/identity/users",
    ]);
  });

  it("returns the original 401 when the refresh fails, rather than looping", async () => {
    const calls: string[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.includes("/api/auth/refresh")) return json({ ok: false }, 401);
      return json({ error: "authentication required" }, 401);
    }) as typeof fetch;

    const resp = await apiFetch("/api/identity/users");

    // The caller must still see a genuine sign-out; swallowing it would leave the UI stuck.
    expect(resp.status).toBe(401);
    expect(calls.filter((c) => c.includes("/api/auth/refresh"))).toHaveLength(1);
  });

  it("does not refresh a response that is not a 401", async () => {
    const calls: string[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      calls.push(String(input));
      return json({ error: "not authorized" }, 403);
    }) as typeof fetch;

    const resp = await apiFetch("/api/identity/users");

    expect(resp.status).toBe(403);
    expect(calls).toEqual(["/api/identity/users"]);
  });
});

describe("concurrent 401s share one refresh", () => {
  it("sends a single refresh for a burst of requests", async () => {
    let refreshes = 0;
    const expired = new Set<string>();
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/auth/refresh")) {
        refreshes += 1;
        // Resolve on a later tick, so every caller is waiting when it settles — the interleaving
        // that a per-response refresh would get wrong.
        await new Promise((r) => setTimeout(r, 5));
        return json({ ok: true });
      }
      if (!expired.has(url)) {
        expired.add(url);
        return json({ error: "authentication required" }, 401);
      }
      return json({ ok: true });
    }) as typeof fetch;

    const results = await Promise.all([
      apiFetch("/api/a"),
      apiFetch("/api/b"),
      apiFetch("/api/c"),
      apiFetch("/api/d"),
    ]);

    expect(results.map((r) => r.status)).toEqual([200, 200, 200, 200]);
    expect(refreshes).toBe(1);
  });
});

describe("the auth routes never trigger a refresh", () => {
  it.each([
    "/api/auth/refresh",
    "/api/auth/srp/begin",
    "/api/auth/srp/finish",
    "/api/auth-check",
    "/api/auth/logout",
  ])("%s", async (path) => {
    const calls: string[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      calls.push(String(input));
      return json({ error: "authentication failed" }, 401);
    }) as typeof fetch;

    const resp = await apiFetch(path, { method: "POST" });

    expect(resp.status).toBe(401);
    // Exactly one call: refreshing here is either a loop or a pointless retry of a failed login.
    expect(calls).toEqual([path]);
  });
});
