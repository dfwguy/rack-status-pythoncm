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
 */
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { srpLogin } from "../lib/srpClient";
import { AuthContext, type AuthState } from "./AuthContext";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({
    authenticated: false,
    user: null,
    loading: true,
  });

  useEffect(() => {
    fetch("/api/auth-check")
      .then((res) => {
        if (res.ok) return res.json();
        throw new Error("not authenticated");
      })
      .then((data) => {
        setState({
          authenticated: true,
          user: data.user,
          loading: false,
          // Carried through the mount check as well as login, so a page reload cannot walk past
          // the change-password prompt (FR-10a).
          mustChangePassword: Boolean(data.must_change_password),
        });
      })
      .catch(() => {
        setState({ authenticated: false, user: null, loading: false });
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
      setState({
        authenticated: true,
        user: result.user?.display_name || result.user?.login || username,
        loading: false,
        mustChangePassword: Boolean(result.mustChangePassword),
      });
      return { ok: true, mustChangePassword: Boolean(result.mustChangePassword) };
    }
    return { ok: false, error: result.error || "Login failed" };
  }, []);

  const logout = useCallback(async () => {
    // The new endpoint revokes the tokens by `jti` as well as clearing the cookies. Clearing alone
    // would leave a copy taken beforehand valid until it expired.
    await fetch("/api/auth/logout", { method: "POST" });
    setState({ authenticated: false, user: null, loading: false });
  }, []);

  return (
    <AuthContext.Provider value={{ ...state, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}
