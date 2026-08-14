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
 * Theme provider: Kaizen UI is the outer theme owner, MUI is bridged to follow it.
 *
 * Components only, so React Refresh can hot-swap this file. The context object, its hook and the
 * persisted-mode reader live in ThemeContext.ts.
 */
import { useCallback, useMemo, type ReactNode } from "react";
import { ThemeProvider as MuiThemeProvider } from "@mui/material/styles";
import CssBaseline from "@mui/material/CssBaseline";
import {
  ThemeProvider as KuiThemeProvider,
  useTheme as useKuiTheme,
} from "@nvidia/foundations-react-core";
import { getTheme, type ThemeMode } from "../theme";
import { ThemeContext, getInitialMode } from "./ThemeContext";

/** Mirrors the KUI theme into MUI and into our own context, so one toggle drives both. */
const ThemeBridge = ({ children }: { children: ReactNode }) => {
  const { theme, setTheme } = useKuiTheme();

  const mode = (theme === "light" ? "light" : "dark") as ThemeMode;

  const toggleTheme = useCallback(() => {
    const next: ThemeMode = mode === "dark" ? "light" : "dark";
    setTheme(next);
    localStorage.setItem("nvis-rack-theme", next);
  }, [mode, setTheme]);

  const muiTheme = useMemo(() => getTheme(mode), [mode]);
  const contextValue = useMemo(() => ({ mode, toggleTheme }), [mode, toggleTheme]);

  return (
    <ThemeContext.Provider value={contextValue}>
      <MuiThemeProvider theme={muiTheme}>
        <CssBaseline />
        {children}
      </MuiThemeProvider>
    </ThemeContext.Provider>
  );
};

interface ThemeProviderProps {
  children: ReactNode;
}

export const ThemeProvider = ({ children }: ThemeProviderProps) => (
  <KuiThemeProvider theme={getInitialMode()} global>
    <ThemeBridge>{children}</ThemeBridge>
  </KuiThemeProvider>
);
