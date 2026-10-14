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
 * Access tokens are short-lived by design (an hour by default): FR-6 permits a *withdrawn* grant to
 * lag by at most that long, so the TTL is the bound. `/api/auth/refresh` existed from the start and
 * **nothing called it**, so once the token expired every request returned "authentication required" while the page
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

import {
  apiDownload,
  apiFetch,
  ensureFreshSession,
  filenameFromDisposition,
  onSessionLost,
  openApiUrl,
  setAccessExpiry,
} from "./apiFetch";

const originalFetch = globalThis.fetch;

beforeEach(() => {
  vi.resetAllMocks();
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  // Module-level expiry outlives a test; clear it so a tracked deadline cannot leak into the next.
  setAccessExpiry(null);
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

  it("announces the sign-out, so the app stops claiming to be signed in", async () => {
    /**
     * The regression this exists for. `apiFetch` refreshed, failed, returned the 401 — and told
     * nobody. `AuthProvider` had learned the answer once at mount, so a window whose tokens the
     * server no longer accepted kept polling `rack-locks`, `status` and `cluster-overview` every
     * few seconds, 401 every time, indefinitely, while still rendering the dashboard.
     */
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/auth/refresh")) return json({ ok: false }, 401);
      return json({ error: "authentication required" }, 401);
    }) as typeof fetch;

    const told = vi.fn();
    onSessionLost(told);
    try {
      expect((await apiFetch("/api/status")).status).toBe(401);
      expect(told).toHaveBeenCalledTimes(1);
    } finally {
      onSessionLost(null);
    }
  });

  it("stays quiet while the refresh works, so a renewal is not a sign-out", async () => {
    let refreshed = false;
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/auth/refresh")) {
        refreshed = true;
        return json({ ok: true });
      }
      return refreshed ? json({ ok: true }) : json({ error: "expired" }, 401);
    }) as typeof fetch;

    const told = vi.fn();
    onSessionLost(told);
    try {
      expect((await apiFetch("/api/status")).status).toBe(200);
      expect(told).not.toHaveBeenCalled();
    } finally {
      onSessionLost(null);
    }
  });

  it("does not announce a sign-out for the auth routes themselves", async () => {
    // A failed login must not be reported as a lost session: it would flip state the login page
    // is already responsible for, mid-attempt.
    globalThis.fetch = (async () => json({ ok: false }, 401)) as typeof fetch;

    const told = vi.fn();
    onSessionLost(told);
    try {
      expect((await apiFetch("/api/auth/srp/finish", { method: "POST" })).status).toBe(401);
      expect(told).not.toHaveBeenCalled();
    } finally {
      onSessionLost(null);
    }
  });

  it("does not refresh a response that is not a 401", async () => {
    const calls: string[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      calls.push(String(input));
      return json({ error: "Access Denied. Contact administrator to request access to this resource", reason: "policy" }, 403);
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

describe("a known-expired token is renewed before the request is sent", () => {
  it("refreshes first, so the dead request is never sent (two round trips, not three)", async () => {
    const calls: string[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.includes("/api/auth/refresh")) {
        return json({ ok: true, access_expires_at: Date.now() / 1000 + 3600 });
      }
      return json({ ok: true });
    }) as typeof fetch;

    // A deadline already in the past (well past the skew window).
    setAccessExpiry(Date.now() / 1000 - 10);

    const resp = await apiFetch("/api/identity/users");

    expect(resp.status).toBe(200);
    // Refresh precedes the request, and the request is sent exactly once — no wasted 401 round trip.
    expect(calls).toEqual(["/api/auth/refresh", "/api/identity/users"]);
  });

  it("sends straight away when the tracked token is still valid", async () => {
    const calls: string[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      calls.push(String(input));
      return json({ ok: true });
    }) as typeof fetch;

    // Comfortably in the future.
    setAccessExpiry(Date.now() / 1000 + 3600);

    const resp = await apiFetch("/api/status");

    expect(resp.status).toBe(200);
    expect(calls).toEqual(["/api/status"]);
  });

  it("does not pre-refresh the auth routes themselves", async () => {
    const calls: string[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      calls.push(String(input));
      return json({ ok: true });
    }) as typeof fetch;

    setAccessExpiry(Date.now() / 1000 - 10);

    await apiFetch("/api/auth/refresh", { method: "POST" });

    // The refresh route must not pre-refresh itself: that is the infinite regress NO_REFRESH guards.
    expect(calls).toEqual(["/api/auth/refresh"]);
  });

  it("updates the tracked deadline from the refresh response", async () => {
    const future = Math.floor(Date.now() / 1000) + 3600;
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/auth/refresh")) return json({ ok: true, access_expires_at: future });
      return json({ ok: true });
    }) as typeof fetch;

    // Start expired so the first call refreshes; that refresh reports a fresh, future deadline.
    setAccessExpiry(Date.now() / 1000 - 10);
    await apiFetch("/api/one");

    // The second call sees the updated (future) deadline and sends without pre-refreshing.
    const calls: string[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      calls.push(String(input));
      return json({ ok: true });
    }) as typeof fetch;
    await apiFetch("/api/two");

    expect(calls).toEqual(["/api/two"]);
  });
});

describe("the auth routes never trigger a refresh", () => {
  it.each([
    "/api/auth/refresh",
    "/api/auth/srp/begin",
    "/api/auth/srp/finish",
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

describe("/api/auth-check", () => {
  /**
   * This route was on the no-refresh list, and taking it off is the fix for a real defect: the
   * one call that asks "am I signed in?" was the one call that could not renew. The access
   * cookie expires hourly while the refresh cookie lasts days, so on almost any page load the
   * access token is dead and the session is still alive — and `AuthProvider` would show the
   * login screen without ever presenting the refresh token. Session length collapsed to the
   * access TTL on every reload, whatever `RACK_WEB_REFRESH_TTL_SECONDS` said.
   */
  it("renews and retries, so a live session survives a reload", async () => {
    const calls: string[] = [];
    let checked = 0;
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.includes("/api/auth/refresh")) return json({ ok: true });
      // Expired access cookie on the first ask, renewed by the second.
      checked += 1;
      return checked === 1
        ? json({ authenticated: false }, 401)
        : json({ authenticated: true, user: "user:admin" });
    }) as typeof fetch;

    const resp = await apiFetch("/api/auth-check");

    expect(resp.status).toBe(200);
    expect(await resp.json()).toMatchObject({ authenticated: true });
    expect(calls).toEqual(["/api/auth-check", "/api/auth/refresh", "/api/auth-check"]);
  });

  it("still reports a sign-out when the refresh token is also dead", async () => {
    const calls: string[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      calls.push(String(input));
      return json({ authenticated: false }, 401);
    }) as typeof fetch;

    const resp = await apiFetch("/api/auth-check");

    // The original 401 is returned untouched, which is what `AuthProvider` reads as signed out.
    expect(resp.status).toBe(401);
    // One renewal attempt, not a loop: the refresh route is itself on the no-refresh list.
    expect(calls).toEqual(["/api/auth-check", "/api/auth/refresh"]);
  });
});

/**
 * The helpers for requests that are not a plain JSON `apiFetch`: downloads, new tabs and the
 * terminal WebSocket. Each existed as a raw `fetch` or `window.open` that worked until the access
 * token expired and then failed for good, because only `apiFetch` can renew it.
 */
describe("apiDownload", () => {
  let clicked: { href: string; download: string }[];
  let createObjectURL: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    clicked = [];
    createObjectURL = vi.fn(() => "blob:mock");
    Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicked.push({ href: this.getAttribute("href") ?? "", download: this.download });
    });
  });

  afterEach(() => {
    onSessionLost(null);
    vi.restoreAllMocks();
  });

  it("renews an expired session and downloads under the server's filename", async () => {
    const calls: string[] = [];
    let served = 0;
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.includes("/api/auth/refresh")) return json({ ok: true });
      served += 1;
      if (served === 1) return json({ error: "authentication required" }, 401);
      return new Response("xlsx-bytes", {
        status: 200,
        headers: { "Content-Disposition": 'attachment; filename="rack-export-2026.xlsx"' },
      });
    }) as typeof fetch;

    await apiDownload("/api/parser-export-excel?log_dir=x", "fallback.xlsx");

    expect(calls).toEqual([
      "/api/parser-export-excel?log_dir=x",
      "/api/auth/refresh",
      "/api/parser-export-excel?log_dir=x",
    ]);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(clicked).toEqual([{ href: "blob:mock", download: "rack-export-2026.xlsx" }]);
  });

  it("falls back to the given name when the server sends none", async () => {
    globalThis.fetch = (async () => new Response("zip-bytes", { status: 200 })) as typeof fetch;

    await apiDownload("/api/export-logs-zip", "rack-logs.zip");

    expect(clicked).toEqual([{ href: "blob:mock", download: "rack-logs.zip" }]);
  });

  it("throws the server's error text and downloads nothing", async () => {
    globalThis.fetch = (async () => json({ ok: false, error: "no exportable .txt logs" }, 404)) as typeof fetch;

    await expect(apiDownload("/api/export-logs-zip", "rack-logs.zip")).rejects.toThrow(
      "no exportable .txt logs",
    );
    expect(clicked).toEqual([]);
  });

  it("signs out and downloads nothing when the refresh also fails", async () => {
    const lost = vi.fn();
    onSessionLost(lost);
    globalThis.fetch = (async () => json({ error: "authentication required" }, 401)) as typeof fetch;

    await apiDownload("/api/parser-export-excel", "x.xlsx");

    expect(lost).toHaveBeenCalledTimes(1);
    expect(clicked).toEqual([]);
    expect(createObjectURL).not.toHaveBeenCalled();
  });
});

describe("filenameFromDisposition", () => {
  it.each([
    ['attachment; filename="report.pdf"', "report.pdf"],
    ["attachment; filename=report.csv", "report.csv"],
    ["attachment; filename*=UTF-8''rack%20g01.zip", "rack g01.zip"],
    [`attachment; filename="plain.txt"; filename*=UTF-8''pr%C3%A9f%C3%A9r%C3%A9.txt`, "préféré.txt"],
  ])("%s -> %s", (header, expected) => {
    expect(filenameFromDisposition(header)).toBe(expected);
  });

  it("returns null when there is no filename", () => {
    expect(filenameFromDisposition(null)).toBeNull();
    expect(filenameFromDisposition("inline")).toBeNull();
  });
});

describe("ensureFreshSession", () => {
  afterEach(() => onSessionLost(null));

  it("is true after renewing an expired access token", async () => {
    let checked = 0;
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      if (String(input).includes("/api/auth/refresh")) return json({ ok: true });
      checked += 1;
      return checked === 1 ? json({ authenticated: false }, 401) : json({ authenticated: true });
    }) as typeof fetch;

    expect(await ensureFreshSession()).toBe(true);
  });

  it("is false, and signs out, when the session is gone", async () => {
    const lost = vi.fn();
    onSessionLost(lost);
    globalThis.fetch = (async () => json({ authenticated: false }, 401)) as typeof fetch;

    expect(await ensureFreshSession()).toBe(false);
    expect(lost).toHaveBeenCalledTimes(1);
  });

  it("is false on a network failure rather than throwing", async () => {
    globalThis.fetch = (async () => {
      throw new TypeError("Failed to fetch");
    }) as typeof fetch;

    expect(await ensureFreshSession()).toBe(false);
  });
});

describe("openApiUrl", () => {
  afterEach(() => {
    onSessionLost(null);
    vi.restoreAllMocks();
  });

  function fakeTab() {
    return { opener: {} as unknown, location: { href: "about:blank" }, close: vi.fn() };
  }

  it("opens the tab inside the click, then navigates it once the session is fresh", async () => {
    const tab = fakeTab();
    const open = vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    let resolveCheck: (r: Response) => void = () => {};
    globalThis.fetch = (() => new Promise<Response>((r) => { resolveCheck = r; })) as typeof fetch;

    const pending = openApiUrl("/terminal?target=head");
    // Synchronously — before the session check resolves — so the popup keeps the user gesture.
    expect(open).toHaveBeenCalledWith("about:blank", "_blank");
    expect(tab.opener).toBeNull();
    expect(tab.location.href).toBe("about:blank");

    resolveCheck(json({ authenticated: true }));
    await pending;
    expect(tab.location.href).toBe("/terminal?target=head");
    expect(tab.close).not.toHaveBeenCalled();
  });

  it("closes the blank tab when the session is gone", async () => {
    const tab = fakeTab();
    vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    globalThis.fetch = (async () => json({ authenticated: false }, 401)) as typeof fetch;

    await openApiUrl("/api/openapi.json");

    expect(tab.close).toHaveBeenCalledTimes(1);
    expect(tab.location.href).toBe("about:blank");
  });
});
