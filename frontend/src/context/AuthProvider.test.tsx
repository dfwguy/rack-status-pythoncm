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
 * `userLogin` must be the bare enrolled login on **both** entry paths.
 *
 * This is the other half of the change-password bug. `ChangePasswordPage.test.tsx` covers the call
 * site — that the page forwards what it was given — but the defect was upstream: the provider put a
 * display name in state after a fresh sign-in and a principal after a page reload, and the page
 * dutifully forwarded either one to SRP.
 *
 * The two paths produce differently-shaped values from the server, so they need separate assertions.
 * Testing only one would have left the other broken, which is precisely what happened.
 */

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as srpClient from "../lib/srpClient";
import { useAuth } from "./AuthContext";
import { AuthProvider } from "./AuthProvider";

// Top-level, not `vi.doMock` inside a test: the provider imports `srpLogin` at module load, so a
// late mock does not reach the binding it already holds.
vi.mock("../lib/srpClient");

function Probe() {
  const { user, userLogin, mustChangePassword, authenticated } = useAuth();
  return (
    <div>
      <span data-testid="user">{user ?? "(none)"}</span>
      <span data-testid="userLogin">{userLogin ?? "(none)"}</span>
      <span data-testid="mustChange">{String(Boolean(mustChangePassword))}</span>
      <span data-testid="authenticated">{String(authenticated)}</span>
    </div>
  );
}

const originalFetch = globalThis.fetch;

beforeEach(() => {
  vi.resetAllMocks();
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("the mount-time credential check", () => {
  it("strips the principal prefix to recover the login", async () => {
    // `/api/auth-check` answers with the principal, not the login.
    globalThis.fetch = (async () =>
      new Response(
        JSON.stringify({ authenticated: true, user: "user:admin", must_change_password: true }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      )) as typeof fetch;

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );

    await waitFor(() =>
      // Exact, not substring: `toHaveTextContent("admin")` also passes against "user:admin",
      // which is exactly the value the bug produced.
      expect(screen.getByTestId("userLogin").textContent).toBe("admin"),
    );
    // The display value keeps the principal; only `userLogin` is normalised.
    expect(screen.getByTestId("user")).toHaveTextContent("user:admin");
  });

  it("never leaves a principal string in userLogin", async () => {
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ authenticated: true, user: "user:someone" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })) as typeof fetch;

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );

    await waitFor(() =>
      expect(screen.getByTestId("userLogin").textContent).toBe("someone"),
    );
  });

  it("carries must_change_password, so a reload cannot skip the prompt", async () => {
    globalThis.fetch = (async () =>
      new Response(
        JSON.stringify({ authenticated: true, user: "user:admin", must_change_password: true }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      )) as typeof fetch;

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("mustChange")).toHaveTextContent("true"));
  });

  it("reports nobody when the check refuses", async () => {
    globalThis.fetch = (async () =>
      new Response("{}", { status: 401 })) as typeof fetch;

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("userLogin")).toHaveTextContent("(none)"));
  });
});

describe("a fresh sign-in", () => {
  it("puts the login in userLogin and the display name in user", async () => {
    // The exact shape that caused the bug: the bootstrap admin's display name is "Administrator",
    // and that is what used to reach SRP.
    vi.mocked(srpClient.srpLogin).mockResolvedValue({
      ok: true,
      user: { login: "admin", display_name: "Administrator", principal: "user:admin" },
      mustChangePassword: true,
    });
    globalThis.fetch = (async () => new Response("{}", { status: 401 })) as typeof fetch;

    function LoginProbe() {
      const { login, user, userLogin } = useAuth();
      return (
        <div>
          <button onClick={() => void login("admin", "pw")}>go</button>
          <span data-testid="user">{user ?? "(none)"}</span>
          <span data-testid="userLogin">{userLogin ?? "(none)"}</span>
        </div>
      );
    }

    render(
      <AuthProvider>
        <LoginProbe />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("userLogin")).toHaveTextContent("(none)"));

    fireEvent.click(screen.getByRole("button", { name: "go" }));

    await waitFor(() => expect(screen.getByTestId("userLogin").textContent).toBe("admin"));
    // The display name is what a human sees; it must not be what SRP is given.
    expect(screen.getByTestId("user")).toHaveTextContent("Administrator");
  });
});

describe("proactive renewal before the access token expires", () => {
  it("hits /api/auth/refresh on a cadence derived from the reported TTL", async () => {
    // A short TTL so the 80% renewal point is reachable with a bounded fake-timer advance.
    const ACCESS_TTL = 1000; // seconds
    const refreshCalls: string[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/auth/refresh")) {
        refreshCalls.push(url);
        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }
      if (url.includes("/api/auth-check")) {
        return new Response(
          JSON.stringify({ authenticated: true, user: "user:admin", access_ttl_seconds: ACCESS_TTL }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    }) as typeof fetch;

    // Fake timers from the start: the renewal interval has to be created *under* them, since fake
    // timers only govern timers created after they are installed.
    vi.useFakeTimers();
    try {
      render(
        <AuthProvider>
          <Probe />
        </AuthProvider>,
      );
      // Flush the async mount check so the provider is authenticated and the interval is armed.
      // (The check resolves on the microtask queue, which fake timers do not freeze.)
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      expect(screen.getByTestId("authenticated")).toHaveTextContent("true");

      expect(refreshCalls).toHaveLength(0);
      // Just short of the 80% mark (800s): no renewal yet.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ACCESS_TTL * 0.8 * 1000 - 1000);
      });
      expect(refreshCalls).toHaveLength(0);
      // Past it: the token is renewed well before the full TTL could lapse.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000);
      });
      expect(refreshCalls).toHaveLength(1);
      // And it keeps renewing, so the session survives the whole refresh-token window.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(ACCESS_TTL * 0.8 * 1000);
      });
      expect(refreshCalls).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("a session that dies while the window is open", () => {
  it("stops claiming to be signed in, so the gate shows the login screen", async () => {
    /**
     * The other half of the `onSessionLost` wiring, driven through the real `apiFetch` rather
     * than by calling the handler directly — the two together are the whole fix, and a test that
     * invoked the callback itself would pass even if the provider never registered it.
     *
     * Before this, the mount-time check was the only thing that ever set `authenticated`, so a
     * window whose refresh token had expired (one week by default) never found out: it kept
     * polling 401s behind a dashboard that looked fine.
     */
    let sessionAlive = true;
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      if (!sessionAlive) {
        // Everything refuses, including the refresh — which is what "the session is gone"
        // means, as opposed to an access token that merely needs renewing.
        return new Response(JSON.stringify({ error: "authentication required" }), { status: 401 });
      }
      if (String(input).includes("/api/auth-check")) {
        return new Response(
          JSON.stringify({ ok: true, principal: "user:admin", display_name: "Admin" }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    }) as typeof fetch;

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("authenticated")).toHaveTextContent("true"));

    sessionAlive = false;
    const { apiFetch } = await import("../lib/apiFetch");
    // Any ordinary call — this is one of the pollers that used to 401 for minutes on end.
    expect((await apiFetch("/api/status")).status).toBe(401);

    await waitFor(() => expect(screen.getByTestId("authenticated")).toHaveTextContent("false"));
    expect(screen.getByTestId("userLogin")).toHaveTextContent("(none)");
  });
});
