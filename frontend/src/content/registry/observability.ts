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
 * Knowledge entries for the Observability domain (Metrics, Logs, Events) and
 * the scope explorer/tree used to filter them by cluster/rack/host.
 */
import type { HelpContent } from "../types";

export const observabilityContent: HelpContent[] = [
  {
    id: "obsMetrics",
    kind: "view",
    title: "Metrics",
    domain: "Observability",
    routePath: "/observability/metrics",
    summary: "Live GPU, platform, and exporter time series from VictoriaMetrics.",
    description:
      "The Metrics page renders the live chart catalog returned by Rack Status' read-only VictoriaMetrics API. Use the rack and time-window selectors to inspect GPU temperature, power, utilization, NVLink, exporter, and infrastructure series. Raw BCM labels remain the query keys, while the UI displays short rack monikers such as d01, e01, f01, g01, and h01. An empty chart is reported as no data; Rack Status never substitutes a zero or mock series.",
    features: [
      "Live Chart.js time-series panels backed by VictoriaMetrics.",
      "Rack and 15-minute, 1-hour, 6-hour, or 24-hour window filters.",
      "Operator-facing short rack monikers while preserving raw query labels.",
      "Explicit upstream-health, query-error, loading, and no-data states.",
    ],
    howTo: [
      {
        task: "Inspect live metrics for a rack",
        steps: [
          "Open Observability > Metrics.",
          "Confirm the VictoriaMetrics health banner reports live DCGM hosts.",
          "Choose a short rack moniker from the Rack selector and select the required window.",
          "Treat an empty or failed chart as missing evidence, not as a zero measurement.",
        ],
      },
    ],
    related: ["obsLogs", "obsEvents"],
    keywords: [
      "metrics",
      "telemetry",
      "victoriametrics",
      "time-series",
      "gpu",
      "nvlink",
      "power",
      "thermal",
      "rack",
      "observability",
    ],
    aria: {
      label: "Metrics page",
      description: "Live VictoriaMetrics charts filtered by rack and time window.",
    },
    aiContext:
      "The Metrics page renders live VictoriaMetrics GPU, NVLink, power, thermal, exporter, and infrastructure series with rack and time-window filters. It displays short rack monikers but queries the raw label. Empty, stale, and failed telemetry must never be described as healthy or replaced with mock values.",
  },
  {
    id: "obsLogs",
    kind: "view",
    title: "Logs",
    domain: "Observability",
    routePath: "/observability/logs",
    summary: "Centralized logs scoped by cluster, rack, or host (preview).",
    description:
      "The Logs page shows centralized log streams that you can scope to a cluster, rack, or host using the scope explorer, for cross-node log inspection. It is currently a preview backed by mock data.",
    features: ["Centralized log view.", "Scope by cluster/rack/host.", "Preview banner indicating mock data."],
    howTo: [{ task: "Scope logs to a host", steps: ["Open Observability > Logs.", "Select the host in the scope explorer.", "Read the scoped logs."] }],
    related: ["obsMetrics", "obsEvents", "scopeExplorer", "tailLogDialog"],
    keywords: ["logs", "centralized", "scope", "observability", "preview"],
    aria: { label: "Logs page", description: "Centralized logs scoped by cluster, rack, or host using the scope explorer (currently mock data)." },
    aiContext:
      "The Logs page shows centralized logs scoped by cluster/rack/host via the scope explorer. Currently a preview backed by mock data.",
  },
  {
    id: "obsEvents",
    kind: "view",
    title: "Events",
    domain: "Observability",
    routePath: "/observability/events",
    summary: "Cluster events and alerts scoped by cluster, rack, or host (preview).",
    description:
      "The Events page shows cluster events and alerts that you can scope to a cluster, rack, or host using the scope explorer, for spotting incidents and state changes. It is currently a preview backed by mock data.",
    features: ["Event/alert timeline.", "Scope by cluster/rack/host.", "Preview banner indicating mock data."],
    howTo: [{ task: "Scope events to a rack", steps: ["Open Observability > Events.", "Select the rack in the scope explorer.", "Review the scoped events."] }],
    related: ["obsMetrics", "obsLogs", "scopeExplorer"],
    keywords: ["events", "alerts", "incidents", "scope", "observability", "preview"],
    aria: { label: "Events page", description: "Cluster events and alerts scoped by cluster, rack, or host using the scope explorer (currently mock data)." },
    aiContext:
      "The Events page shows cluster events/alerts scoped by cluster/rack/host via the scope explorer. Currently a preview backed by mock data.",
  },
  {
    id: "scopeExplorer",
    kind: "component",
    title: "Scope explorer",
    domain: "Observability",
    summary: "Side panel + content area to filter observability by scope.",
    description:
      "ScopeExplorer is the shared layout for the observability pages: a scope side panel (the scope tree) plus a content area that filters Metrics/Logs/Events by the selected cluster, rack, or host.",
    features: ["Scope side panel + content area.", "Filters Metrics/Logs/Events by selection.", "Shared across observability pages."],
    howTo: [{ task: "Change the active scope", steps: ["Open any observability page.", "Pick a node in the scope tree.", "The content area filters to that scope."] }],
    related: ["scopeTree", "obsMetrics", "obsLogs", "obsEvents"],
    keywords: ["scope", "explorer", "filter", "cluster", "rack", "host", "observability"],
    aria: { label: "Scope explorer", description: "Side panel and content area that filter observability data by cluster, rack, or host." },
    aiContext:
      "ScopeExplorer is the observability layout: a scope side panel (scope tree) plus a content area that filters Metrics/Logs/Events by selected cluster/rack/host.",
  },
  {
    id: "scopeTree",
    kind: "component",
    title: "Scope tree",
    domain: "Observability",
    summary: "Tree for cluster → rack → host selection.",
    description: "ScopeTree is the cluster → rack → host selection tree inside the scope explorer used to choose the active observability scope.",
    features: ["Hierarchical cluster/rack/host tree.", "Drives the active scope."],
    howTo: [{ task: "Select a host", steps: ["Expand the cluster and rack in the tree.", "Click the host to scope to it."] }],
    related: ["scopeExplorer"],
    keywords: ["scope", "tree", "hierarchy", "cluster", "rack", "host", "selection"],
    aria: { label: "Scope tree", description: "Hierarchical tree for selecting a cluster, rack, or host as the observability scope." },
    aiContext: "ScopeTree is the cluster→rack→host selection tree inside ScopeExplorer that sets the active observability scope.",
  },
];
