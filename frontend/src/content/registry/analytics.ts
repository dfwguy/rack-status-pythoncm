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
    summary: "Rack-level and node-level analytics, plus the cluster and customer-schema reports.",
    description:
      "The Analytics page has two tabs. Rack Level aggregates rack and benchmark data into cluster-wide charts and tables — validation pass rates, NCCL/NEMO benchmark trends, and comparisons across racks. Node Level shows a heatmap with one square per node, coloured by a selectable data view (node up/down, test completion, or any bring-up benchmark that produces a metric such as NCCL busbw), with each node's up/down state always drawn as the square's border. The Compact layout tiles racks eight across as blocks of trays so a whole floor fits on one screen; Linear gives each rack its own row and shows a load gauge per node. The heatmap can be downloaded as a PNG or JPEG image. The Rack Level tab carries a report generator offering two reports: the Rack Status standard report (cluster details plus a row per node, as CSV, JSON, or a PDF with rack-level charts) and the Nscale TX JSON schema report (the customer's Consolidated Test Plan contract, schema-validated before download).",
    features: [
      "Rack Level: cluster-wide aggregate charts, benchmark trends, cross-rack comparisons.",
      "Node Level: per-node heatmap grouped by rack, with a data-view selector.",
      "Compact layout tiles racks as blocks with a per-rack tally; Linear shows a gauge per node.",
      "Test completion view: all tests done, partial, or none run — independent of pass/fail.",
      "Up/down state shown as each cell's border on every view.",
      "Metric views scale against the fleet's observed range, stated in the legend.",
      "Download image: the heatmap as a report-ready PNG or JPEG with title, legend and timestamp.",
      "Report generator (Rack Level tab): the Rack Status report as CSV/JSON/PDF, or the Nscale TX schema report as validated JSON. Both sweep every node for hardware and firmware identity, with a progress bar.",
    ],
    howTo: [
      { task: "Compare benchmark results across racks", steps: ["Open Analytics & Benchmarks > Analytics.", "Stay on the Rack Level tab.", "Select the metric/benchmark.", "Read the aggregate chart and rack comparison."] },
      { task: "Find slow or failed nodes across the cluster", steps: ["Open Analytics & Benchmarks > Analytics.", "Switch to the Node Level tab.", "Pick a benchmark in the data-view selector.", "Hover a square for that node's value and verdict; pale cells are failures or tests that never ran."] },
      { task: "See how far bring-up has got", steps: ["Open Analytics & Benchmarks > Analytics.", "Switch to the Node Level tab.", "Choose the Test completion view.", "Green racks are fully tested, red have not started, yellow are part-way; the per-rack tally counts each."] },
      { task: "Put the heatmap in a status email", steps: ["Open Analytics & Benchmarks > Analytics.", "Switch to the Node Level tab.", "Pick the view and layout you want to show.", "Choose PNG or JPEG and click Download image — the file carries the cluster name, legend and timestamp."] },
      { task: "Produce a report", steps: ["Open Analytics & Benchmarks > Analytics.", "Stay on the Rack Level tab and find Generate a report.", "Pick the report, then the format (CSV, JSON or PDF; the Nscale schema report is JSON only).", "Click Generate & download and watch the per-node sweep progress."] },
    ],
    related: ["reports", "nodes", "parserResultsDialog", "dashboard"],
    keywords: ["analytics", "aggregate", "benchmark", "trends", "comparison", "charts", "heatmap", "node level", "report", "csv report", "pdf report", "json report", "nscale schema", "test completion", "download image", "png", "jpeg", "compact layout"],
    aria: { label: "Analytics page", description: "Rack-level and node-level analytics with downloadable cluster reports." },
    aiContext:
      "The Analytics page has a Rack Level tab (cluster-wide aggregate charts: validation pass rates, NCCL/NEMO trends, cross-rack comparisons) and a Node Level tab (per-node heatmap grouped by rack; the fill is a selectable view — node up/down, test completion, or a bring-up metric like NCCL busbw — and the border always shows up/down). Test completion counts a test as done once it has a verdict, pass or fail, so it answers 'has this node been tested' rather than 'did it pass'. Two layouts: Compact tiles racks eight across as tray blocks with a per-rack good/warn/bad tally (the whole floor on one screen), Linear gives each rack a row and each node a load gauge. Metric views auto-scale to the fleet's observed range. Download image renders the heatmap to a light-background PNG/JPEG with title, legend, cluster and timestamp baked in, drawn on a canvas so it does not depend on the UI theme. The Rack Level tab carries a report generator with two reports. The Rack Status standard report renders one dataset as CSV, JSON, or a PDF with an executive summary and rack-level charts. The Nscale TX JSON schema report answers the customer's Consolidated Test Plan contract and is validated against the vendored schema before download, so it is JSON only; fields bring-up cannot measure are marked TBD for the operator to edit. Both reports share one fleet sweep of per-node hardware and firmware identity — asking twice while a sweep runs joins the running job rather than starting a second one — and report progress per node. PDF is only offered when the image can render it.",
  },
  {
    id: "postRunNodeReport",
    kind: "view",
    title: "Validation Status",
    domain: "Analytics & Benchmarks",
    routePath: "/analytics/post-run-nodes",
    summary: "Floor-style node validation participation, failure trends, deep audits, and scheduler status.",
    description:
      "Validation Status combines live bring-up checks, current Slurm state, durable validation activities, exact scheduler targets, and deep-audit findings. The default All view spans every discovered validation domain, while selectors isolate Node Health, DCGM, NCCL, NeMo, Llama, MNUberGEMM, and source-specific jobs. Selecting a node shows Job IDs, checks, metrics, failure reasons, evidence timestamps, and the complete validation history.",
    features: [
      "Punchcard-style floor view grouped by rack, with run and attributed-failure counts per node.",
      "Increasing, decreasing, flat, and new failure trends; a repeat flag requires at least three findings across three distinct runs.",
      "Node drill-down with every validation participation, Job ID, scheduler state, evidence category, and finding.",
      "Current scheduler state and reason shown beside validation history.",
      "Persistent Node hardware status and Node network status report sections.",
      "One-node hardware or NVLink/network deep audits can be launched from the investigation dialog.",
      "Operator findings can be recorded locally or explicitly attached to a scheduler drain after confirmation.",
    ],
    howTo: [
      { task: "Investigate a validation failure", steps: ["Open Analytics & Benchmarks > Validation Status.", "Select All or a validation domain.", "Hover a node for current evidence, then select it.", "Review checks, Job IDs, metrics, failure reasons, and current scheduler comment.", "Launch the relevant hardware or NVLink/network audit and watch the 30-second live view refresh."] },
      { task: "Record and drain a confirmed node issue", steps: ["Open the node from the floor view.", "Choose an evidence category and enter the exact reason.", "Enable the scheduler drain option.", "Confirm the exact node; Rack Status records the finding and applies the generated scheduler reason."] },
    ],
    tips: ["A failed multi-node job is visible as participation but does not count against every tray. A node failure requires explicit evidence, a single-node job, or a structured audit/operator finding."],
    related: ["analytics", "reports", "nodes", "singleRack"],
    keywords: ["post run", "node history", "punchcard", "trend", "repeat failure", "slurm reason", "hardware audit", "network audit", "nvlink", "nccl", "job id"],
    aria: { label: "Validation Status page", description: "Live SU, rack, and node validation map with domain filters, job evidence, predictions, and downloadable reports." },
    aiContext:
      "This page is the first-class post-run node history, not a Google punchcard import. It joins durable validation activities to exact scheduler targets, snapshots node membership, counts attributed failures only with node-specific evidence, flags repeat offenders across distinct runs, and shows current scheduler state/reason. It has persistent hardware and NVLink/network report sections. A selected node can launch either deep audit or receive an operator mark; draining requires an explicit confirmation.",
  },
  {
    id: "reports",
    kind: "view",
    title: "Reports",
    domain: "Analytics & Benchmarks",
    routePath: "/analytics/reports",
    summary: "Download saved cluster reports, plus bug report and NVLMapper archives.",
    description:
      "The Reports page holds two separate things. Generated reports are the cluster and customer-schema documents produced from Analytics > Rack Level; each is saved when it finishes, so you can start a report, leave the page, and download it here later — a fleet sweep takes minutes and is no longer lost by navigating away. Rows show the report type, format, when it was generated, the node count it covers, and any schema errors, and can be downloaded or deleted. Below that, the bug report section lists NVIDIA bug report archives, NVLMapper outputs and Excel exports already present in the reports directory on this dashboard host.",
    features: [
      "Generated Rack Status (CSV/JSON/PDF) and Nscale TX schema (JSON) reports, saved automatically when they finish.",
      "Reports survive leaving the page and a container restart; the most recent are kept and older ones pruned.",
      "Schema errors are shown on the row, so a report that failed validation is visible without regenerating it.",
      "Separate listing of nvidia-bug-report archives, NVLMapper CSV/XLS and parser Excel exports from the reports directory.",
    ],
    howTo: [
      {
        task: "Get a report you generated earlier",
        steps: [
          "Open Analytics & Benchmarks > Reports.",
          "Find the row under Generated reports — newest first.",
          "Click Download.",
        ],
      },
      {
        task: "Generate a new report",
        steps: [
          "Go to Analytics & Benchmarks > Analytics, Rack Level tab.",
          "Use Generate a report, pick the report and format, and start it.",
          "You can leave the page; it appears here when the sweep finishes.",
        ],
      },
    ],
    related: ["analytics", "dashboard"],
    keywords: ["reports", "report", "export", "summary", "handoff", "validation", "benchmark", "saved reports", "pdf", "csv", "bug report", "nvlmapper", "download"],
    aria: { label: "Reports page", description: "Download saved cluster reports and node bug report archives." },
    aiContext:
      "Analytics > Reports lists (1) generated reports from report_store.py, served by GET /api/reports/generated with download and DELETE at /api/reports/generated/<id>, kept under var/reports on the rack-data volume so they survive restarts, and (2) bug report / NVLMapper files from the reports directory via /api/bug-report-inventory. Reports are archived by the node-report worker when it completes, not when downloaded, so closing the tab mid-sweep no longer loses the run. Generation itself happens on Analytics > Analytics, Rack Level tab.",
  },
];
