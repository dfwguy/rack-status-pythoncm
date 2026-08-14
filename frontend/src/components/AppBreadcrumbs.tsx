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
import { useLocation } from "react-router-dom";
import Box from "@mui/material/Box";
import { Breadcrumbs } from "@nvidia/foundations-react-core";
import { pathToRoute, rackIdFromPath, RACKS_PATH } from "../routes";

type Crumb = string | { children: ReactNode };

/**
 * Global breadcrumb bar (rendered by AppLayout under the toolbar) that shows
 * the current location as `Domain / View [/ Rack]`, derived from the URL.
 * Renders nothing for single-level destinations (e.g. Settings) per the KUI
 * guidance against breadcrumbs with no parent to navigate back to.
 */
export function AppBreadcrumbs() {
  const location = useLocation();
  const route = pathToRoute(location.pathname);

  if (!route || route.viewId === "settings") return null;

  const rackId = rackIdFromPath(location.pathname);

  const items: Crumb[] = [route.domain];
  if (rackId) {
    items.push({ children: <a href={`#${RACKS_PATH}`}>{route.label}</a> });
    items.push(rackId);
  } else {
    items.push(route.label);
  }

  return (
    <Box
      sx={{
        px: 3,
        py: 1,
        display: "flex",
        alignItems: "center",
        minHeight: 40,
        bgcolor: "background.paper",
        borderBottom: 1,
        borderColor: "divider",
      }}
    >
      <Breadcrumbs items={items} size="small" />
    </Box>
  );
}
