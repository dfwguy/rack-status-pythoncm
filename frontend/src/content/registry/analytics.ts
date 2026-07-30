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
    summary: "Rack-level and node-level analytics, plus the downloadable daily report.",
    description:
      "The Analytics page has two tabs. Rack Level aggregates rack and benchmark data into cluster-wide charts and tables — validation pass rates, NCCL/NEMO benchmark trends, and comparisons across racks. Node Level shows a heatmap with one square per node in the cluster, coloured by a selectable data view (node up/down, or any bring-up benchmark that produces a metric such as NCCL busbw), with each node's up/down state always drawn as the square's border. A Download report button on either tab produces the daily node-validation report as schema-validated JSON.",
    features: [
      "Rack Level: cluster-wide aggregate charts, benchmark trends, cross-rack comparisons.",
      "Node Level: per-node heatmap grouped by rack, with a data-view selector.",
      "Up/down state shown as each cell's border on every view.",
      "Metric views scale against the fleet's observed range, stated in the legend.",
      "Download report: daily node-validation JSON, validated against the report schema.",
    ],
    howTo: [
      { task: "Compare benchmark results across racks", steps: ["Open Analytics & Benchmarks > Analytics.", "Stay on the Rack Level tab.", "Select the metric/benchmark.", "Read the aggregate chart and rack comparison."] },
      { task: "Find slow or failed nodes across the cluster", steps: ["Open Analytics & Benchmarks > Analytics.", "Switch to the Node Level tab.", "Pick a benchmark in the data-view selector.", "Hover a square for that node's value and verdict; pale cells are failures or tests that never ran."] },
      { task: "Produce the daily report", steps: ["Open Analytics & Benchmarks > Analytics.", "Click Download report.", "Edit the TBD firmware and test-plan fields before sending it on."] },
    ],
    related: ["reports", "nodes", "parserResultsDialog", "dashboard"],
    keywords: ["analytics", "aggregate", "benchmark", "trends", "comparison", "charts", "heatmap", "node level", "daily report", "json report"],
    aria: { label: "Analytics page", description: "Rack-level and node-level analytics with a downloadable daily report." },
    aiContext:
      "The Analytics page has a Rack Level tab (cluster-wide aggregate charts: validation pass rates, NCCL/NEMO trends, cross-rack comparisons) and a Node Level tab (per-node heatmap grouped by rack; the fill is a selectable view — node up/down or a bring-up metric like NCCL busbw — and the border always shows up/down). Metric views auto-scale to the fleet's observed range. A Download report button on either tab returns the daily node-validation report as JSON validated against the vendored report schema; fields bring-up cannot measure (firmware versions, test plan reference) are marked TBD for the operator to edit.",
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
