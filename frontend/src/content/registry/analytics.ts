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
      "The Analytics page has two tabs. Rack Level aggregates rack and benchmark data into cluster-wide charts and tables — validation pass rates, NCCL/NEMO benchmark trends, and comparisons across racks. Node Level shows a heatmap with one square per node, coloured by a selectable data view (node up/down, test completion, or any bring-up benchmark that produces a metric such as NCCL busbw), with each node's up/down state always drawn as the square's border. The Compact layout tiles racks eight across as blocks of trays so a whole floor fits on one screen; Linear gives each rack its own row and shows a load gauge per node. The heatmap can be downloaded as a PNG or JPEG image, and a Download report button on either tab produces the daily node-validation report as schema-validated JSON.",
    features: [
      "Rack Level: cluster-wide aggregate charts, benchmark trends, cross-rack comparisons.",
      "Node Level: per-node heatmap grouped by rack, with a data-view selector.",
      "Compact layout tiles racks as blocks with a per-rack tally; Linear shows a gauge per node.",
      "Test completion view: all tests done, partial, or none run — independent of pass/fail.",
      "Up/down state shown as each cell's border on every view.",
      "Metric views scale against the fleet's observed range, stated in the legend.",
      "Download image: the heatmap as a report-ready PNG or JPEG with title, legend and timestamp.",
      "Download report: daily node-validation JSON, validated against the report schema.",
    ],
    howTo: [
      { task: "Compare benchmark results across racks", steps: ["Open Analytics & Benchmarks > Analytics.", "Stay on the Rack Level tab.", "Select the metric/benchmark.", "Read the aggregate chart and rack comparison."] },
      { task: "Find slow or failed nodes across the cluster", steps: ["Open Analytics & Benchmarks > Analytics.", "Switch to the Node Level tab.", "Pick a benchmark in the data-view selector.", "Hover a square for that node's value and verdict; pale cells are failures or tests that never ran."] },
      { task: "See how far bring-up has got", steps: ["Open Analytics & Benchmarks > Analytics.", "Switch to the Node Level tab.", "Choose the Test completion view.", "Green racks are fully tested, red have not started, yellow are part-way; the per-rack tally counts each."] },
      { task: "Put the heatmap in a status email", steps: ["Open Analytics & Benchmarks > Analytics.", "Switch to the Node Level tab.", "Pick the view and layout you want to show.", "Choose PNG or JPEG and click Download image — the file carries the cluster name, legend and timestamp."] },
      { task: "Produce the daily report", steps: ["Open Analytics & Benchmarks > Analytics.", "Click Download report.", "Edit the TBD firmware and test-plan fields before sending it on."] },
    ],
    related: ["reports", "nodes", "parserResultsDialog", "dashboard"],
    keywords: ["analytics", "aggregate", "benchmark", "trends", "comparison", "charts", "heatmap", "node level", "daily report", "json report", "test completion", "download image", "png", "jpeg", "compact layout"],
    aria: { label: "Analytics page", description: "Rack-level and node-level analytics with a downloadable daily report." },
    aiContext:
      "The Analytics page has a Rack Level tab (cluster-wide aggregate charts: validation pass rates, NCCL/NEMO trends, cross-rack comparisons) and a Node Level tab (per-node heatmap grouped by rack; the fill is a selectable view — node up/down, test completion, or a bring-up metric like NCCL busbw — and the border always shows up/down). Test completion counts a test as done once it has a verdict, pass or fail, so it answers 'has this node been tested' rather than 'did it pass'. Two layouts: Compact tiles racks eight across as tray blocks with a per-rack good/warn/bad tally (the whole floor on one screen), Linear gives each rack a row and each node a load gauge. Metric views auto-scale to the fleet's observed range. Download image renders the heatmap to a light-background PNG/JPEG with title, legend, cluster and timestamp baked in, drawn on a canvas so it does not depend on the UI theme. A Download report button on either tab returns the daily node-validation report as JSON validated against the vendored report schema; fields bring-up cannot measure (firmware versions, test plan reference) are marked TBD for the operator to edit.",
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
