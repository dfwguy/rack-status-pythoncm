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
  user: string | null;
  loading: boolean;
}

export interface AuthContextValue extends AuthState {
  login: (password: string) => Promise<{ ok: boolean; error?: string }>;
  logout: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue>({
  authenticated: false,
  user: null,
  loading: true,
  login: async () => ({ ok: false }),
  logout: async () => {},
});

export const useAuth = () => useContext(AuthContext);
