/**
 * Knowledge entries for the Analytics & Benchmarks domain (Analytics, Reports).
 */
import type { HelpContent } from "../types";

export const analyticsContent: HelpContent[] = [
  {
    id: "analytics",
    kind: "view",
    title: "Analytics",
    domain: "Analytics & Benchmarks",
    routePath: "/analytics/overview",
    summary: "Aggregate rack analytics and benchmark trends across the cluster.",
    description:
      "The Analytics page aggregates rack and benchmark data into cluster-wide charts and tables — validation pass rates, NCCL/NEMO benchmark trends, and comparisons across racks — to spot outliers and track campaign progress.",
    features: ["Cluster-wide aggregate charts.", "Benchmark trend analysis.", "Cross-rack comparisons."],
    howTo: [
      { task: "Compare benchmark results across racks", steps: ["Open Analytics & Benchmarks > Analytics.", "Select the metric/benchmark.", "Read the aggregate chart and rack comparison."] },
    ],
    related: ["reports", "parserResultsDialog", "dashboard"],
    keywords: ["analytics", "aggregate", "benchmark", "trends", "comparison", "charts"],
    aria: { label: "Analytics page", description: "Aggregate rack analytics and benchmark trends across the cluster." },
    aiContext:
      "The Analytics page aggregates rack/benchmark data into cluster-wide charts and tables (validation pass rates, NCCL/NEMO trends, cross-rack comparisons) to spot outliers and track progress.",
  },
  {
    id: "reports",
    kind: "view",
    title: "Reports",
    domain: "Analytics & Benchmarks",
    routePath: "/analytics/reports",
    summary: "Generate and export validation/benchmark reports.",
    description:
      "The Reports page produces shareable validation and benchmark reports summarising a rack or campaign's results for handoff to customers or stakeholders.",
    features: ["Generate validation/benchmark reports.", "Export for sharing/handoff."],
    howTo: [{ task: "Generate a report", steps: ["Open Analytics & Benchmarks > Reports.", "Choose the scope/report type.", "Generate and export."] }],
    related: ["analytics", "dashboard"],
    keywords: ["reports", "report", "export", "summary", "handoff", "validation", "benchmark"],
    aria: { label: "Reports page", description: "Generate and export validation and benchmark reports." },
    aiContext: "The Reports page generates and exports shareable validation/benchmark reports for a rack or campaign.",
  },
];
