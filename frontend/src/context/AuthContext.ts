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
 * Auth context object and hook — no components, so React Refresh can fast-refresh the provider.
 *
 * Consumers keep importing `useAuth` from this path; the provider moved to AuthProvider.tsx.
 * See ThemeContext.ts for why the split goes this way round.
 */
import { createContext, useContext } from "react";

export interface AuthState {
  authenticated: boolean;
  /** What to show a human. A display name, a login, or a principal — never fed to a computation. */
  user: string | null;
  /**
   * The bare login (`admin`), and the only value SRP may be given.
   *
   * Named `userLogin` rather than `login` because `login` is already the sign-in
   * *method* on this context, and a field colliding with it does not compile.
   *
   * Separate from `user` because conflating them was a real bug: SRP derives
   * `x = H(salt, H(login ":" password))`, so the identity string has to be byte-identical to the
   * one the server enrolled. `user` held a *display name* after a fresh login (`"Administrator"`
   * for the bootstrap admin) and a *principal* after a reload (`"user:admin"`), and either one
   * produced a valid-looking proof the server could only reject — surfacing as "current password is
   * incorrect" with a password that was perfectly correct.
   */
  userLogin?: string | null;
  /** True until the mount-time credential check has answered. */
  loading: boolean;
  /** The bootstrap credential is flagged for change (FR-10a); the UI should insist on it. */
  mustChangePassword?: boolean;
  /**
   * No project has been configured, so the setup wizard should run.
   *
   * Carried on the auth state rather than fetched by the view that needs it, for the same
   * reason `mustChangePassword` is: the gate has to decide what to render *before* any view
   * mounts, and a second request at that point is a blank screen with a spinner on every load.
   */
  setupRequired?: boolean;
}

export interface AuthContextValue extends AuthState {
  /**
   * SRP login. Takes a username because there are users now — the previous signature took only a
   * password, which was the shape of a single shared secret.
   */
  login: (
    username: string,
    password: string,
  ) => Promise<{ ok: boolean; error?: string; mustChangePassword?: boolean }>;
  logout: () => Promise<void>;
  /**
   * The wizard finished; stop gating on it.
   *
   * Local, because the server flag was already written by `/api/setup/complete` — re-reading
   * it would be a round trip to learn what we just caused.
   */
  markSetupComplete: () => void;
}

export const AuthContext = createContext<AuthContextValue>({
  authenticated: false,
  user: null,
  loading: true,
  login: async () => ({ ok: false }),
  logout: async () => {},
  markSetupComplete: () => {},
});

export const useAuth = () => useContext(AuthContext);
