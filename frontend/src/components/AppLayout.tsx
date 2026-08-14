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
import type { ReactNode } from "react";
import Box from "@mui/material/Box";
import { Divider, Text } from "@nvidia/foundations-react-core";

interface AppLayoutProps {
  sidebar: ReactNode;
  toolbar: ReactNode;
  children: ReactNode;
  /** Optional breadcrumb bar rendered between the toolbar and main content. */
  breadcrumbs?: ReactNode;
  backendMode?: string;
}

export function AppLayout({ sidebar, toolbar, children, breadcrumbs, backendMode }: AppLayoutProps) {
  return (
    <Box sx={{ display: "flex", minHeight: "100vh", overflow: "hidden" }}>
      {sidebar}

      <Box sx={{ flexGrow: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        {toolbar}
        {breadcrumbs}

        <Box component="main" sx={{ flexGrow: 1, bgcolor: "background.default", overflow: "auto" }}>
          {children}
        </Box>

        <Divider />
        <Box
          component="footer"
          sx={{
            py: 1.5,
            px: 3,
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            bgcolor: "background.paper",
          }}
        >
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            NVIS Rack Status
          </Text>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            {backendMode ? `backend: ${backendMode}` : ""}
          </Text>
        </Box>
      </Box>
    </Box>
  );
}
