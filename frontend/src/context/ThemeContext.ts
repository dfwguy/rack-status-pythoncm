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
 * Theme context object and its hook — no components, deliberately.
 *
 * Split out of ThemeContext.tsx so that file exports only the provider component. A module that
 * exports both a component and a hook forces React Refresh to do a full page reload on every
 * edit instead of a fast refresh, and the same rule catches exporting the context object.
 *
 * The import path is unchanged for consumers (`../context/ThemeContext`), which is why the hook
 * lives here and the provider moved to ThemeProvider.tsx rather than the other way round: the
 * hook has dozens of callers and the provider has one.
 */
import { createContext, useContext } from "react";
import type { ThemeMode } from "../theme";

export interface ThemeContextValue {
  mode: ThemeMode;
  toggleTheme: () => void;
}

export const ThemeContext = createContext<ThemeContextValue>({
  mode: "dark",
  toggleTheme: () => {},
});

export const useThemeMode = () => useContext(ThemeContext);

/** Persisted theme choice, or dark. Read once when the provider mounts. */
export const getInitialMode = (): ThemeMode => {
  try {
    const stored = localStorage.getItem("nvis-rack-theme");
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    // Private browsing / storage disabled: fall through to the default.
  }
  return "dark";
};
