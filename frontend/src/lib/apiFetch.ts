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
 * Thin wrapper around the browser ``fetch`` that injects the rack-operator headers
 * (display name + stable client id) on every request. Use this in place of raw ``fetch``
 * anywhere the SPA talks to the Flask backend so server-side rack-lock tracking can
 * attribute operations to the right operator.
 */
import { getOperatorName } from "./getOperatorName";

const CLIENT_ID_KEY = "nvis-rack-client-id";

let _clientId: string | null = null;

/** Stable per-browser client id (matches the existing one used by useRackStatus). */
export function getClientId(): string {
  if (_clientId) return _clientId;
  try {
    let cached = localStorage.getItem(CLIENT_ID_KEY);
    if (!cached) {
      cached = `spa-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
      localStorage.setItem(CLIENT_ID_KEY, cached);
    }
    _clientId = cached;
  } catch {
    _clientId = `spa-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  }
  return _clientId;
}

/**
 * Paths that must never trigger a refresh attempt.
 *
 * Each for its own reason, and the reasons are not interchangeable:
 *
 * * `/api/auth/refresh` — renewing in response to a failed renewal is an infinite regress.
 * * `/api/auth/srp/` — the exchange that *obtains* a session; there is nothing yet to renew.
 * * `/api/auth/logout` — renewing a session in order to end it is the wrong direction.
 *
 * **`/api/auth-check` used to be on this list and its removal is a bug fix.** It cannot loop: it
 * is not the refresh route, so refresh-then-retry terminates. Listing it meant the one place that
 * asks "am I signed in?" was the one place that could not renew — so on a page load with an
 * expired access cookie and a perfectly valid refresh token, `AuthProvider` saw a 401, concluded
 * "not authenticated" and showed the login screen. The refresh token was never tried. That made
 * the session length effectively the *access* TTL (one hour) on every reload, no matter how long
 * `RACK_WEB_REFRESH_TTL_SECONDS` was set to.
 */
const NO_REFRESH = ["/api/auth/refresh", "/api/auth/srp/", "/api/auth/logout"];

/**
 * One shared in-flight refresh.
 *
 * A dashboard fires a dozen requests at once, so an expiring token produces a dozen simultaneous
 * 401s. Refreshing per response would send a dozen refreshes, and — because each rotates state —
 * most would race and lose. All callers await the same promise instead.
 */
let refreshInFlight: Promise<boolean> | null = null;

/**
 * When the current access token expires, in epoch milliseconds — or `null` when unknown.
 *
 * The SPA cannot read the token: it is an `HttpOnly` cookie, invisible to JavaScript by design. So
 * the deadline is *tracked* from values the backend hands back — the `exp` claim on `auth-check`
 * and the expiry on a refresh — rather than parsed from the token itself. `null` until the first of
 * those arrives, during which `apiFetch` simply behaves as it did before: send, and renew on a 401.
 */
let accessExpiresAt: number | null = null;

/** Refresh this many ms *before* the tracked expiry, to cover client/server clock skew. */
const EXPIRY_SKEW_MS = 30_000;

/**
 * Record when the access token expires. `AuthProvider` calls this with the `access_expires_at` an
 * `auth-check` reported (seconds); `refreshSession` calls it from the refresh response. Passing a
 * non-finite value clears the estimate, falling back to the reactive path.
 */
export function setAccessExpiry(epochSeconds: number | null | undefined): void {
  accessExpiresAt =
    typeof epochSeconds === "number" && Number.isFinite(epochSeconds) ? epochSeconds * 1000 : null;
}

/** Whether the tracked access token is expired (or within the skew window of it). */
function accessTokenLikelyExpired(): boolean {
  return accessExpiresAt !== null && Date.now() >= accessExpiresAt - EXPIRY_SKEW_MS;
}

/**
 * Told when a 401 survives the refresh — i.e. the session is genuinely gone.
 *
 * The docstring below has always said the caller "and ultimately `AuthProvider`" sees a real
 * sign-out. It did not: nothing was wired, so `AuthProvider` learned the answer once at mount and
 * never again. A window whose tokens the server had stopped accepting therefore kept polling —
 * `rack-locks`, `status` and `cluster-overview` every few seconds, 401 every time, indefinitely,
 * while the UI still looked signed in.
 *
 * Observed on a desktop window left open for a day. The trigger is the **refresh token
 * expiring** — one week by default, so any window open longer than that, and every window that
 * never authenticated in its own cookie jar. A server restart is *not* the cause: the signing
 * secret is persisted under `var/secrets/`, so tokens survive one. (An earlier version of this
 * note blamed restarts, which sent the next reader looking in the wrong place.)
 *
 * A single module-level handler rather than an event bus: there is exactly one consumer, and the
 * point is that it is impossible to forget to register it — `AuthProvider` does so at mount.
 */
let sessionLost: (() => void) | null = null;

/** Register the sign-out handler. `AuthProvider` owns this; passing `null` unregisters. */
export function onSessionLost(handler: (() => void) | null): void {
  sessionLost = handler;
}

/**
 * Renew the access token from the refresh cookie. Shared by the reactive 401 path below, the
 * proactive timer `AuthProvider` drives, and the pre-check in `apiFetch` — so all of them collapse
 * onto the one in-flight promise rather than racing refreshes that each rotate state.
 */
export function refreshSession(): Promise<boolean> {
  if (refreshInFlight === null) {
    refreshInFlight = fetch("/api/auth/refresh", {
      method: "POST",
      credentials: "same-origin",
    })
      .then(async (res) => {
        if (!res.ok) return false;
        // Track the new token's expiry so the next request's pre-check is accurate. A body that
        // cannot be read or lacks the field just leaves the estimate untouched — the reactive 401
        // path still covers correctness.
        try {
          const data = await res.clone().json();
          if (data && data.access_expires_at != null) setAccessExpiry(Number(data.access_expires_at));
        } catch {
          // Non-JSON or already-consumed body: ignore, keep the prior estimate.
        }
        return true;
      })
      .catch(() => false)
      .finally(() => {
        refreshInFlight = null;
      });
  }
  return refreshInFlight;
}

/**
 * ``fetch`` replacement: adds the operator/client headers, and renews an expired session once.
 *
 * **The renewal is why this wrapper is worth having.** Access tokens last an hour by default
 * (`ACCESS_TTL_SECONDS` in `backend/identity/tokens.py`) — short, because FR-6 lets a *withdrawn*
 * grant lag by at most that long. Without a renewal path the UI simply broke when the token expired:
 * every call returned "authentication required" while the page still looked signed in, because the
 * mount-time check had passed long before. That is what a refresh token is for.
 *
 * **Every browser request to `/api` goes through here** — JSON, streamed bodies, downloads
 * ({@link apiDownload}), and anything the browser opens itself ({@link ensureFreshSession} first).
 * A request that bypasses it still carries the cookie, so it works until the token expires and
 * then fails for good. ESLint enforces this (`frontend/eslint.config.js`).
 *
 * On a 401 it refreshes once and retries. If the refresh fails the original 401 is returned
 * untouched, so the caller — and ultimately `AuthProvider` — still sees a genuine sign-out rather
 * than a silent retry loop.
 *
 * **It also renews *before* sending when it can see the token has expired.** The access token lives
 * in an `HttpOnly` cookie the SPA cannot read, but the backend hands back its expiry as metadata, so
 * `apiFetch` knows the deadline without the token. When that deadline has passed it refreshes first
 * and sends once — turning the expired-token case from three round trips (request → 401, refresh,
 * retry) into two (refresh, request). The reactive 401 path below stays as the backstop for the
 * cases the pre-check cannot see: an unknown deadline, clock skew, or a server-side revocation.
 */
export async function apiFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const url = typeof input === "string" ? input : input instanceof URL ? input.pathname : input.url;
  const mayRefresh = !NO_REFRESH.some((p) => url.includes(p));

  // Pre-emptive renewal: skip the request we already know would 401.
  if (mayRefresh && accessTokenLikelyExpired()) {
    await refreshSession();
  }

  const first = await rawFetch(input, init);
  if (first.status !== 401) return first;

  if (!mayRefresh) return first;

  if (!(await refreshSession())) {
    // The session is gone, not merely stale. Say so once, here, rather than leaving every caller
    // to invent its own answer: the ad-hoc `window.location.href = "/login?next=..."` in the
    // views loses the route (this app is a HashRouter, and those handlers read `pathname +
    // search`), and the pollers had no handler at all.
    sessionLost?.();
    return first;
  }
  // The body of `init` may be a stream that the first attempt consumed. Callers in this codebase
  // pass strings, which are replayable; a stream body would need cloning before the first send.
  return rawFetch(input, init);
}

/**
 * Renew the session if it has expired, and say whether it is still valid.
 *
 * For requests the browser makes itself — `window.open` of an `/api` URL, the `/terminal` page,
 * the terminal WebSocket. They carry the cookie but cannot run {@link apiFetch}'s refresh, so an
 * expired access token fails them outright. Call this first; it goes through `apiFetch`, so a
 * failed renewal also triggers the app-wide sign-out.
 */
export async function ensureFreshSession(): Promise<boolean> {
  try {
    return (await apiFetch("/api/auth-check")).ok;
  } catch {
    return false;
  }
}

/**
 * Open a same-origin URL that needs a session (an `/api` URL, `/terminal`) in a new tab.
 *
 * The tab is opened **synchronously**, inside the click that called this, and only navigated once
 * the session is known to be fresh: a `window.open` issued after an `await` has lost the user
 * gesture, and popup blockers refuse it. If the session is gone the blank tab is closed and
 * `apiFetch` has already signed the UI out.
 *
 * `noopener` cannot be passed to `window.open` here — it makes the call return `null`, leaving
 * nothing to navigate — so the opener link is cut by hand instead, to the same effect.
 *
 * Call it directly from the event handler, before any other `await`.
 */
export async function openApiUrl(url: string): Promise<void> {
  const win = window.open("about:blank", "_blank");
  if (win) win.opener = null;
  if (!(await ensureFreshSession())) {
    win?.close();
    return;
  }
  if (win) win.location.href = url;
}

/** The filename from a `Content-Disposition` header, preferring the RFC 5987 `filename*` form. */
export function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;
  const extended = /filename\*\s*=\s*[^']*''([^;]+)/i.exec(header);
  if (extended) {
    try {
      return decodeURIComponent(extended[1].trim().replace(/^"|"$/g, ""));
    } catch {
      // Malformed percent-encoding: fall through to the plain form.
    }
  }
  const plain = /filename\s*=\s*("([^"]*)"|[^;]+)/i.exec(header);
  if (!plain) return null;
  return (plain[2] ?? plain[1]).trim() || null;
}

/**
 * Download an `/api` response as a file, with the session renewal `apiFetch` provides.
 *
 * Replaces `window.open` and a bare `fetch` → blob, both of which fail permanently once the access
 * token expires. Throws with the server's `error` text on a non-OK response. A 401 that survives
 * the refresh returns quietly: `apiFetch` has already signed the UI out.
 */
export async function apiDownload(
  input: string,
  fallbackName: string,
  init?: RequestInit,
): Promise<void> {
  const res = await apiFetch(input, init);
  if (res.status === 401) return;
  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { error?: string };
      if (body?.error) message = body.error;
    } catch {
      // Not JSON: keep the status line.
    }
    throw new Error(message);
  }
  const blob = await res.blob();
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = filenameFromDisposition(res.headers.get("Content-Disposition")) ?? fallbackName;
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoked on the next tick: revoking synchronously can cancel the download in some browsers.
  setTimeout(() => URL.revokeObjectURL(href), 0);
}

function rawFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const op = getOperatorName();
  const cid = getClientId();
  const headers = new Headers(init?.headers || {});
  if (op && !headers.has("X-Rack-Operator")) {
    headers.set("X-Rack-Operator", op);
  }
  if (cid && !headers.has("X-Rack-Client-Id")) {
    headers.set("X-Rack-Client-Id", cid);
  }
  return fetch(input, {
    credentials: "same-origin",
    ...init,
    headers,
  });
}
