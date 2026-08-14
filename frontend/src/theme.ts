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
import { createTheme, type Theme } from "@mui/material/styles";

const NVIDIA_GREEN = "#76B900";

const sharedTypography = {
  fontFamily: '"Inter", "Roboto", "Helvetica Neue", Arial, sans-serif',
  h4: { fontWeight: 700 },
  h5: { fontWeight: 600 },
  h6: { fontWeight: 600 },
};

const sharedShape = { borderRadius: 8 };

const sharedComponents = {
  MuiButton: {
    styleOverrides: {
      root: { textTransform: "none" as const, fontWeight: 600 },
    },
  },
  MuiPaper: {
    styleOverrides: {
      root: { backgroundImage: "none" },
    },
  },
};

const darkTheme = createTheme({
  palette: {
    mode: "dark",
    primary: {
      main: NVIDIA_GREEN,
      light: "#8FCE2A",
      dark: "#5A8F00",
      contrastText: "#000000",
    },
    secondary: { main: "#888888", light: "#AAAAAA", dark: "#666666" },
    background: { default: "#121212", paper: "#1E1E1E" },
    text: { primary: "#FFFFFF", secondary: "#B0B0B0" },
    divider: "rgba(255, 255, 255, 0.12)",
    error: { main: "#FF5252" },
    warning: { main: "#FFB74D" },
    info: { main: "#64B5F6" },
    success: { main: NVIDIA_GREEN },
  },
  typography: sharedTypography,
  shape: sharedShape,
  components: {
    ...sharedComponents,
    MuiAppBar: {
      styleOverrides: {
        root: { backgroundColor: "#1A1A1A", backgroundImage: "none" },
      },
    },
    MuiCard: {
      styleOverrides: {
        root: { backgroundColor: "#242424", border: "1px solid #2E2E2E" },
      },
    },
    MuiDrawer: {
      styleOverrides: {
        paper: {
          backgroundColor: "#1A1A1A",
          borderRight: "1px solid rgba(255, 255, 255, 0.12)",
        },
      },
    },
    MuiTableHead: {
      styleOverrides: {
        root: {
          "& .MuiTableCell-head": { backgroundColor: "#2E2E2E", fontWeight: 700 },
        },
      },
    },
  },
});

const lightTheme = createTheme({
  palette: {
    mode: "light",
    primary: {
      main: "#5A8F00",
      light: NVIDIA_GREEN,
      dark: "#3D6200",
      contrastText: "#FFFFFF",
    },
    secondary: { main: "#666666", light: "#888888", dark: "#444444" },
    background: { default: "#F5F5F5", paper: "#FFFFFF" },
    text: { primary: "#1A1A1A", secondary: "#555555" },
    divider: "rgba(0, 0, 0, 0.12)",
    error: { main: "#D32F2F" },
    warning: { main: "#ED6C02" },
    info: { main: "#0288D1" },
    success: { main: "#5A8F00" },
  },
  typography: sharedTypography,
  shape: sharedShape,
  components: {
    ...sharedComponents,
    MuiAppBar: {
      styleOverrides: {
        root: {
          backgroundColor: "#FFFFFF",
          backgroundImage: "none",
          color: "#1A1A1A",
        },
      },
    },
    MuiCard: {
      styleOverrides: {
        root: { backgroundColor: "#FFFFFF", border: "1px solid #E0E0E0" },
      },
    },
    MuiDrawer: {
      styleOverrides: {
        paper: {
          backgroundColor: "#FFFFFF",
          borderRight: "1px solid rgba(0, 0, 0, 0.12)",
        },
      },
    },
    MuiTableHead: {
      styleOverrides: {
        root: {
          "& .MuiTableCell-head": { backgroundColor: "#F5F5F5", fontWeight: 700 },
        },
      },
    },
  },
});

export type ThemeMode = "light" | "dark";

const themes: Record<ThemeMode, Theme> = { dark: darkTheme, light: lightTheme };

export const getTheme = (mode: ThemeMode): Theme => themes[mode];

export default darkTheme;
