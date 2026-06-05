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
    summary: "Time-series metrics scoped by cluster, rack, or host (preview).",
    description:
      "The Metrics page shows time-series telemetry (GPU, NVLink, power, thermal) that you can scope using the scope explorer to a cluster, rack, or host. It is currently a preview backed by mock data.",
    features: ["Time-series metric charts.", "Scope by cluster/rack/host via the scope explorer.", "Preview banner indicating mock data."],
    howTo: [
      {
        task: "Scope metrics to a rack",
        steps: ["Open Observability > Metrics.", "Use the scope explorer to select a cluster/rack/host.", "The charts update to that scope."],
      },
    ],
    related: ["obsLogs", "obsEvents", "scopeExplorer", "scopeTree"],
    keywords: ["metrics", "telemetry", "time-series", "gpu", "power", "thermal", "observability", "preview"],
    aria: { label: "Metrics page", description: "Time-series metrics scoped by cluster, rack, or host using the scope explorer (currently mock data)." },
    aiContext:
      "The Metrics page shows time-series telemetry (GPU/NVLink/power/thermal) scoped by cluster/rack/host via the scope explorer. Currently a preview backed by mock data.",
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
