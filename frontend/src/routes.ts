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
import type { ViewId } from "./components/Sidebar";

/**
 * Single source of truth mapping each navigable `ViewId` to its hash path and
 * its domain grouping (per docs/UX_FRAMEWORK.md). Reused by the router,
 * sidebar highlight, toolbar title, AIChat context, and breadcrumbs.
 */
export interface RouteDef {
  viewId: ViewId;
  /** Hash path without the leading `#` (e.g. "/compute/racks"). */
  path: string;
  /** Domain label for the breadcrumb trail (Compute, Infrastructure, ...). */
  domain: string;
  /** Human-readable view label for breadcrumb/title. */
  label: string;
}

/** Default landing route; "/" and unknown paths redirect here. */
export const DEFAULT_PATH = "/compute/overview";

/** Racks list path; rack detail nests under it as `/compute/racks/:rackId`. */
export const RACKS_PATH = "/compute/racks";

/** Builds the deep-link path for a single rack's detail view. */
export const rackDetailPath = (rack: string): string =>
  `${RACKS_PATH}/${encodeURIComponent(rack)}`;

export const ROUTES: RouteDef[] = [
  { viewId: "dashboard", path: DEFAULT_PATH, domain: "Compute", label: "Cluster overview" },
  { viewId: "nodes", path: "/compute/nodes", domain: "Compute", label: "Nodes" },
  { viewId: "singleRack", path: RACKS_PATH, domain: "Compute", label: "Racks" },
  { viewId: "multirack", path: "/compute/multirack", domain: "Compute", label: "Multirack operations" },
  { viewId: "scheduler", path: "/compute/scheduler", domain: "Compute", label: "Scheduler" },
  { viewId: "infraEthernet", path: "/infrastructure/ethernet", domain: "Infrastructure", label: "Ethernet" },
  { viewId: "infraIbNvlink", path: "/infrastructure/ib-nvlink", domain: "Infrastructure", label: "IB / NVLink" },
  { viewId: "infraStorage", path: "/infrastructure/storage", domain: "Infrastructure", label: "Storage" },
  { viewId: "obsMetrics", path: "/observability/metrics", domain: "Observability", label: "Metrics" },
  { viewId: "obsLogs", path: "/observability/logs", domain: "Observability", label: "Logs" },
  { viewId: "obsEvents", path: "/observability/events", domain: "Observability", label: "Events" },
  { viewId: "tools", path: "/tools/operations", domain: "Tools", label: "Operations & Scripts" },
  { viewId: "workflows", path: "/tools/workflows", domain: "Tools", label: "Workflows" },
  { viewId: "terminal", path: "/tools/terminal", domain: "Tools", label: "Terminal" },
  { viewId: "analytics", path: "/analytics/overview", domain: "Analytics & Benchmarks", label: "Analytics" },
  { viewId: "postRunNodeReport", path: "/analytics/post-run-nodes", domain: "Analytics & Benchmarks", label: "Validation Status" },
  { viewId: "reports", path: "/analytics/reports", domain: "Analytics & Benchmarks", label: "Reports" },
  { viewId: "projects", path: "/platform/projects", domain: "Platform", label: "Projects & Clusters" },
  { viewId: "platformCatalog", path: "/platform/catalog", domain: "Platform", label: "Platform Catalog" },
  { viewId: "toolRegistry", path: "/platform/tools", domain: "Platform", label: "Tool Registry" },
  { viewId: "apiReference", path: "/platform/api", domain: "Platform", label: "API Reference" },
  { viewId: "settings", path: "/settings", domain: "Platform", label: "Settings" },
];

const byViewId = new Map<ViewId, RouteDef>(ROUTES.map((r) => [r.viewId, r]));

/** Resolves a `ViewId` to its hash path (falls back to the default route). */
export function viewIdToPath(viewId: ViewId): string {
  return byViewId.get(viewId)?.path ?? DEFAULT_PATH;
}

/** Resolves a `ViewId` to its full route definition. */
export function routeForViewId(viewId: ViewId): RouteDef | undefined {
  return byViewId.get(viewId);
}

/**
 * Matches a pathname to its route definition. Rack detail
 * (`/compute/racks/:rackId`) resolves to the `singleRack` route so the active
 * domain/view stays "Compute / Racks".
 */
export function pathToRoute(pathname: string): RouteDef | undefined {
  const normalized = pathname.replace(/\/+$/, "") || "/";
  if (normalized === RACKS_PATH || normalized.startsWith(`${RACKS_PATH}/`)) {
    return byViewId.get("singleRack");
  }
  return ROUTES.find((r) => r.path === normalized);
}

/** Extracts the rack id from a rack-detail pathname, or null if not a detail path. */
export function rackIdFromPath(pathname: string): string | null {
  const match = pathname.match(/^\/compute\/racks\/(.+)$/);
  return match ? decodeURIComponent(match[1].replace(/\/+$/, "")) : null;
}
