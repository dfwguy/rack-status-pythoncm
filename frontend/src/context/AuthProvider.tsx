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
 * Auth provider: checks the session once on mount, then exposes login/logout.
 *
 * Components only; the context object and `useAuth` live in AuthContext.ts.
 *
 * **The mount check goes through `apiFetch`, not `fetch`, and that is load-bearing.** The access
 * cookie expires hourly while the refresh cookie lasts days, so on most page loads the access
 * token is already dead and the session is still perfectly alive. A raw `fetch` sees the 401 and
 * lands in the `.catch` below — signing the operator out of a live session on every reload. Going
 * through the wrapper renews first and only reports a sign-out when the *refresh* also fails,
 * which is the only 401 that actually means "log in again".
 */
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { srpLogin } from "../lib/srpClient";
import { AuthContext, type AuthState } from "./AuthContext";
import { apiFetch, onSessionLost } from "../lib/apiFetch";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({
    authenticated: false,
    user: null,
    loading: true,
  });

  useEffect(() => {
    apiFetch("/api/auth-check")
      .then((res) => {
        if (res.ok) return res.json();
        throw new Error("not authenticated");
      })
      .then((data) => {
        setState({
          authenticated: true,
          user: data.user,
          // `data.user` is the principal (`user:admin`). Strip the kind prefix to get the login
          // SRP needs; anything else produces a proof the server cannot verify.
          userLogin: String(data.user ?? "").replace(/^user:/, "") || null,
          loading: false,
          // Carried through the mount check as well as login, so a page reload cannot walk past
          // the change-password prompt (FR-10a).
          mustChangePassword: Boolean(data.must_change_password),
          setupRequired: Boolean(data.setup_required),
        });
      })
      .catch(() => {
        setState({ authenticated: false, user: null, userLogin: null, loading: false });
      });
  }, []);

  /**
   * Log in over SRP. The password is used to compute a proof and is never sent.
   *
   * Takes a username now — the old signature was `login(password)` against a single shared
   * secret, which is precisely what this phase replaces. There is no fallback to `/api/login`:
   * that route still answers and still sets a Flask session, but the gate stopped honouring
   * sessions, so falling back would report success and then 401 on every subsequent request.
   */
  const login = useCallback(async (username: string, password: string) => {
    const result = await srpLogin(username, password);
    if (result.ok) {
      // The SRP exchange answers about credentials, so it cannot know whether this deployment
      // has been configured. Ask the same endpoint the mount check uses rather than inventing a
      // second source: without this the wizard would appear only after a page reload, which is
      // exactly the path a first-time operator does not take.
      let setupRequired = false;
      try {
        const res = await apiFetch("/api/auth-check");
        if (res.ok) setupRequired = Boolean((await res.json()).setup_required);
      } catch {
        // A missed wizard prompt is recoverable — it is offered again on the next load, and
        // from the Projects screen. Failing the login over it would not be.
      }
      setState({
        authenticated: true,
        user: result.user?.display_name || result.user?.login || username,
        // The login, not the display name. `username` is what the operator typed, which the server
        // canonicalises — so prefer the server's own value when it sent one.
        userLogin: result.user?.login || username,
        loading: false,
        mustChangePassword: Boolean(result.mustChangePassword),
        setupRequired,
      });
      return { ok: true, mustChangePassword: Boolean(result.mustChangePassword) };
    }
    return { ok: false, error: result.error || "Login failed" };
  }, []);

  const markSetupComplete = useCallback(() => {
    setState((s) => ({ ...s, setupRequired: false }));
  }, []);

  /**
   * A 401 that survived a refresh means the session is gone, so stop claiming otherwise.
   *
   * Without this the mount-time check was the *only* thing that ever set this state, so a window
   * open across a server restart — which invalidates every outstanding token — kept polling 401s
   * every few seconds while still rendering the dashboard. The operator sees stale data and no
   * explanation; the log fills with 401s.
   *
   * Idempotent, and deliberately not a redirect: flipping the state lands on the login screen
   * through the normal gate, and the route the reader was on stays in the router rather than being
   * flattened into a `?next=` that a HashRouter cannot reconstruct.
   */
  useEffect(() => {
    onSessionLost(() => {
      setState((s) =>
        s.authenticated
          ? { ...s, authenticated: false, user: null, userLogin: null, loading: false }
          : s,
      );
    });
    return () => onSessionLost(null);
  }, []);

  const logout = useCallback(async () => {
    // The new endpoint revokes the tokens by `jti` as well as clearing the cookies. Clearing alone
    // would leave a copy taken beforehand valid until it expired.
    await apiFetch("/api/auth/logout", { method: "POST" });
    setState({ authenticated: false, user: null, userLogin: null, loading: false });
  }, []);

  return (
    <AuthContext.Provider value={{ ...state, login, logout, markSetupComplete }}>
      {children}
    </AuthContext.Provider>
  );
}
