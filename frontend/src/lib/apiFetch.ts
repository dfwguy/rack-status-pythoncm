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

/** Paths that must never trigger a refresh attempt, or a failure here becomes a loop. */
const NO_REFRESH = ["/api/auth/refresh", "/api/auth/srp/", "/api/auth-check", "/api/auth/logout"];

/**
 * One shared in-flight refresh.
 *
 * A dashboard fires a dozen requests at once, so an expiring token produces a dozen simultaneous
 * 401s. Refreshing per response would send a dozen refreshes, and — because each rotates state —
 * most would race and lose. All callers await the same promise instead.
 */
let refreshInFlight: Promise<boolean> | null = null;

function refreshSession(): Promise<boolean> {
  if (refreshInFlight === null) {
    refreshInFlight = fetch("/api/auth/refresh", {
      method: "POST",
      credentials: "same-origin",
    })
      .then((res) => res.ok)
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
 * **The renewal is why this wrapper is worth having.** Access tokens last 15 minutes by design —
 * short, because FR-6 lets a *withdrawn* grant lag by at most that long. Without a renewal path the
 * UI simply broke after fifteen minutes: every call returned "authentication required" while the
 * page still looked signed in, because the mount-time check had passed long before. That is what a
 * refresh token is for, and the route existed; nothing called it.
 *
 * On a 401 it refreshes once and retries. If the refresh fails the original 401 is returned
 * untouched, so the caller — and ultimately `AuthProvider` — still sees a genuine sign-out rather
 * than a silent retry loop.
 */
export async function apiFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const first = await rawFetch(input, init);
  if (first.status !== 401) return first;

  const url = typeof input === "string" ? input : input instanceof URL ? input.pathname : input.url;
  if (NO_REFRESH.some((p) => url.includes(p))) return first;

  if (!(await refreshSession())) return first;
  // The body of `init` may be a stream that the first attempt consumed. Callers in this codebase
  // pass strings, which are replayable; a stream body would need cloning before the first send.
  return rawFetch(input, init);
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
