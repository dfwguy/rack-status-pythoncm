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
 * Knowledge entries for the Compute domain: the cluster overview, racks list,
 * multirack operations, the Slurm scheduler view, and the components that make
 * up those pages (rack table, status summary, squeue/sinfo panels, etc.).
 */
import type { HelpContent } from "../types";

export const computeContent: HelpContent[] = [
  {
    id: "dashboard",
    kind: "view",
    title: "Cluster overview",
    domain: "Compute",
    routePath: "/compute/overview",
    summary: "Top-level health and capacity snapshot for the whole cluster.",
    description:
      "The Cluster overview is the landing page. It aggregates validation progress, node and rack counts, GPU capacity, and live scheduler activity into a set of stat tiles plus a cluster-wide squeue summary, so you can judge the state of the deployment at a glance before drilling into a rack.",
    features: [
      "Stat tiles for total racks, nodes, GPUs, and validation progress (done / failed / ongoing / pending).",
      "Cluster metadata (project name, site, platform) with an inline editor.",
      "Cluster-wide squeue overview summarising running and pending jobs.",
      "NVFTP / data sync status indicators.",
      "Refresh control to re-pull live status from the head node.",
    ],
    howTo: [
      {
        task: "Check overall validation progress",
        steps: [
          "Open Compute > Cluster overview.",
          "Read the validation stat tiles (done, failed, ongoing, pending).",
          "Click a count to jump to the Racks list filtered by that state.",
        ],
      },
      {
        task: "Edit cluster metadata",
        steps: [
          "Find the cluster metadata card on the overview.",
          "Click edit, update the project name / site fields, and save.",
          "The overview refreshes to reflect the new metadata.",
        ],
      },
    ],
    faqs: [
      {
        q: "Why are some counts zero in demo mode?",
        a: "When RACK_STATUS_DEMO=1 the app returns synthetic data; counts reflect the mock cluster, not a real head node.",
      },
    ],
    tips: ["Use the Refresh button in the toolbar to re-pull cluster status without reloading the page."],
    related: ["singleRack", "slurm", "statTiles", "clusterSqueueOverview"],
    keywords: ["overview", "dashboard", "cluster", "summary", "capacity", "health", "home", "landing"],
    aria: {
      label: "Cluster overview page",
      description:
        "Top-level cluster dashboard showing validation progress, rack and node counts, GPU capacity, and live scheduler activity.",
    },
    aiContext:
      "The Cluster overview is the landing dashboard. It shows aggregate validation progress (done/failed/ongoing/pending), rack/node/GPU counts, cluster metadata, and a cluster-wide squeue summary. Users come here for a high-level health snapshot before drilling into individual racks.",
  },
  {
    id: "singleRack",
    kind: "view",
    title: "Racks",
    domain: "Compute",
    routePath: "/compute/racks",
    summary: "Sortable, filterable table of every rack and its validation status.",
    description:
      "The Racks page is the main working table. Each row is a rack showing its validation state, latest job, comment, and quick actions. You can sort and filter, click the status summary tags to filter by state, add comments, open parser charts, and click a rack to open its detail view.",
    features: [
      "Sortable/filterable table of all racks with validation state and metadata.",
      "Clickable status summary tags (done/fail/ongoing/pending) that filter rows.",
      "Per-rack row menu: open detail, view parser charts, tail logs, run actions.",
      "Inline comment editing per rack.",
      "Command activity indicator showing in-flight jobs per rack.",
    ],
    howTo: [
      {
        task: "Open a rack's detail view",
        steps: ["Open Compute > Racks.", "Click the rack name (or its row) to open the rack detail view."],
      },
      {
        task: "Filter racks by validation state",
        steps: [
          "Click one of the status summary tags above the table (e.g. Failed).",
          "The table filters to racks in that state.",
          "Click the tag again to clear the filter.",
        ],
      },
      {
        task: "Add or edit a rack comment",
        steps: ["Find the rack's comment cell.", "Click to edit, type your note, and confirm to save."],
      },
    ],
    related: ["rackDetail", "rackTable", "statusSummary", "rackTableCommandIndicator", "workerStatus"],
    keywords: ["racks", "table", "validation", "status", "list", "filter", "sort", "comment"],
    aria: {
      label: "Racks validation table",
      description:
        "Sortable, filterable table of all racks showing validation status, latest job, comments, and per-rack actions.",
    },
    aiContext:
      "The Racks page is the main table listing every rack with its validation state, latest job, and comment. Users sort/filter, click status summary tags to filter by state, edit comments, open parser charts, and click a rack to open its detail view (rackDetail).",
  },
  {
    id: "nodes",
    kind: "view",
    title: "Nodes",
    domain: "Compute",
    routePath: "/compute/nodes",
    summary: "Flat, filterable inventory of every compute node with per-node actions.",
    description:
      "The Nodes page lists every compute node across the cluster in one paginated table (18 rows by default, one NVL72 rack in view). Filter by datacenter row, rack, or node name, then select a single node to run Validation, Hardware, Networking, and Services actions scoped to just that node. Node inventory and up/down state come from the same live status feed the Racks and Rack Detail views use.",
    features: [
      "Paginated node table (default 18 rows) with sortable node names.",
      "Filters for row, rack, and node name.",
      "Per-node status tag (UP / DOWN / INST / UNKNOWN) and BMC/RedFish link.",
      "Select a node to expose Validation, Hardware, Networking, and Services action tabs scoped to that node.",
      "Bring-up results panel: per-job status, metric, and reason from the cluster automation (results + reports), with open-log and NCCL charts per job.",
      "Per-node web terminal.",
    ],
    howTo: [
      {
        task: "Run an action on a single node",
        steps: [
          "Open Compute > Nodes.",
          "Filter by rack or name to find the node.",
          "Click the node row to select it.",
          "Open the relevant action tab (Validation, Hardware, Networking, Services) and confirm.",
        ],
      },
      {
        task: "Narrow the list to one rack",
        steps: ["Use the Rack filter and pick one or more racks.", "The table and pagination update to the matching nodes."],
      },
    ],
    tips: [
      "The default page size is 18 so a single NVL72 rack fits on one page.",
      "Actions here apply to the one selected node; use the Racks view to act on a whole rack.",
    ],
    related: ["singleRack", "rackDetail", "validationTab", "hardwareTab", "networkingTab", "servicesTab"],
    keywords: ["nodes", "node", "tray", "inventory", "filter", "single node", "per-node", "compute"],
    aria: {
      label: "Nodes inventory page",
      description:
        "Paginated table of every compute node with row/rack/name filters; selecting a node exposes per-node Validation, Hardware, Networking, and Services actions.",
    },
    aiContext:
      "The Nodes page is a flat, paginated inventory of every compute node (default 18 rows). Users filter by row, rack, or name and select a single node to run Validation, Hardware, Networking, and Services actions scoped to that node. A Bring-up results panel shows per-job status/metric/reason from the cluster automation (Travis's /home/cmods/bringup results + reports, via /api/bringup/node) with open-log and NCCL charts per job. Node/tray state reuses the same live status feed as the Racks and Rack Detail views.",
  },
  {
    id: "multirack",
    kind: "view",
    title: "Multirack operations",
    domain: "Compute",
    routePath: "/compute/multirack",
    summary: "Run validation and commands across many racks at once.",
    description:
      "Multirack operations lets you select several racks and launch the same validation or command across all of them, then monitor aggregated progress and tail combined logs. It is built for campaign-style validation where you energise and verify many racks in sequence.",
    features: [
      "Select multiple racks and launch a shared validation/command.",
      "Aggregated progress tracking across the selected racks.",
      "Combined log tailing for the multirack run.",
      "Parser results across the selected set.",
    ],
    howTo: [
      {
        task: "Run a validation across multiple racks",
        steps: [
          "Open Compute > Multirack operations.",
          "Select the racks to include.",
          "Choose the validation/command and start the run.",
          "Watch aggregated progress and tail logs as it executes.",
        ],
      },
    ],
    tips: ["Validate one rack at a time when possible; require all nodes healthy before starting a multirack campaign."],
    related: ["singleRack", "parserResultsDialog", "tailLogDialog"],
    keywords: ["multirack", "batch", "campaign", "bulk", "parallel", "validation"],
    aria: {
      label: "Multirack operations page",
      description: "Select multiple racks and run validation or commands across all of them with aggregated progress.",
    },
    aiContext:
      "Multirack operations runs the same validation/command across many selected racks at once, with aggregated progress and combined log tailing. Used for campaign-style validation of many racks.",
  },
  {
    id: "slurm",
    kind: "view",
    title: "Scheduler",
    domain: "Compute",
    routePath: "/compute/scheduler",
    summary: "Live Slurm job queue and node state (squeue + sinfo).",
    description:
      "The Scheduler page surfaces live Slurm state: a squeue panel of running and pending jobs and a sinfo panel of node states (idle, alloc, drain, down). From here you can inspect jobs, tail a job's log, and understand why nodes are unavailable.",
    features: [
      "Live squeue table of running and pending jobs.",
      "Live sinfo table of node states and reasons (sinfo -R --long).",
      "Tail a selected job's log output.",
      "Cross-reference node drain/down reasons with rack validation.",
    ],
    howTo: [
      {
        task: "See why a node is unavailable",
        steps: [
          "Open Compute > Scheduler.",
          "Read the sinfo panel for the node's state (drain/down) and reason.",
          "Address the reason on the rack detail page if it is a hardware/validation issue.",
        ],
      },
      {
        task: "Tail a running job's log",
        steps: ["Find the job in the squeue panel.", "Use the log/tail action on its row to stream output."],
      },
    ],
    related: ["squeuePanel", "sinfoPanel", "squeueDialog", "sinfoDialog"],
    keywords: ["slurm", "scheduler", "squeue", "sinfo", "jobs", "queue", "nodes", "drain", "idle", "alloc"],
    aria: {
      label: "Slurm scheduler page",
      description: "Live Slurm job queue (squeue) and node state (sinfo) with the ability to tail job logs.",
    },
    aiContext:
      "The Scheduler page shows live Slurm state: a squeue panel (running/pending jobs) and a sinfo panel (node states: idle/alloc/drain/down with reasons). Users tail job logs and diagnose unavailable nodes here.",
  },
  {
    id: "rackTable",
    kind: "component",
    title: "Rack table",
    domain: "Compute",
    summary: "The main dashboard table of racks with filters, menus, and charts.",
    description:
      "RackTable renders every rack as a sortable row with validation state, comment, and a row menu. It supports column sorting, text and status filtering, inline comments, command activity indicators, and launching parser charts or rack actions.",
    features: [
      "Column sorting and free-text filtering.",
      "Per-row menu: open detail, parser charts, tail logs, run actions.",
      "Inline comment editor per rack.",
      "Command activity indicator per row.",
    ],
    howTo: [
      {
        task: "Sort the table",
        steps: ["Click a column header to sort by it.", "Click again to reverse the sort direction."],
      },
    ],
    related: ["singleRack", "statusSummary", "rackTableCommandIndicator", "parserResultsDialog"],
    keywords: ["rack table", "grid", "rows", "sort", "filter", "menu"],
    aria: {
      label: "Rack table",
      description: "Sortable, filterable table of racks with per-rack menus, comments, and command activity indicators.",
    },
    aiContext:
      "RackTable is the main racks grid: sortable/filterable rows, per-rack row menu (open detail, parser charts, tail logs, actions), inline comments, and command activity indicators.",
  },
  {
    id: "statusSummary",
    kind: "component",
    title: "Status summary",
    domain: "Compute",
    summary: "Clickable validation tags that filter the rack table.",
    description:
      "StatusSummary shows count tags for each validation state (done, failed, ongoing, pending). Clicking a tag filters the rack table to that state; clicking again clears the filter.",
    features: ["At-a-glance counts per validation state.", "Click to filter the rack table by state."],
    howTo: [
      {
        task: "Filter by validation state",
        steps: ["Click a status tag (e.g. Failed) to filter.", "Click the same tag again to clear."],
      },
    ],
    related: ["singleRack", "rackTable"],
    keywords: ["status", "summary", "tags", "counts", "filter", "done", "failed", "ongoing", "pending"],
    aria: {
      label: "Validation status summary",
      description: "Clickable count tags for each validation state that filter the rack table when selected.",
    },
    aiContext:
      "StatusSummary shows clickable count tags per validation state (done/failed/ongoing/pending) above the rack table; clicking filters the table by that state.",
  },
  {
    id: "rackTableCommandIndicator",
    kind: "component",
    title: "Command activity indicator",
    domain: "Compute",
    summary: "Per-rack dot showing in-flight command activity.",
    description:
      "RackTableCommandIndicator is a small indicator on each rack row that lights up when a command/job is currently running against that rack, driven by the global command store.",
    features: ["Shows active command/job state per rack.", "Updates live as jobs start and finish."],
    howTo: [
      {
        task: "Tell if a rack has a running command",
        steps: ["Look for the activity dot on the rack's row.", "An active dot means a command/job is in flight."],
      },
    ],
    related: ["rackTable"],
    keywords: ["indicator", "activity", "command", "running", "job", "dot"],
    aria: {
      label: "Rack command activity indicator",
      description: "Indicator that shows when a command or job is currently running against a rack.",
    },
    aiContext:
      "RackTableCommandIndicator is a per-rack dot that lights up when a command/job is currently running against that rack (driven by the global command store).",
  },
  {
    id: "workerStatus",
    kind: "component",
    title: "Worker status panel",
    domain: "Compute",
    summary: "Backend worker mode and rack-count note under the table.",
    description:
      "WorkerStatusPanel reports the native worker mode, the number of racks, and a backend note beneath the rack table, so you can see how status is being collected (native workers vs other modes).",
    features: ["Shows native worker mode.", "Shows rack count and a backend note."],
    howTo: [
      {
        task: "Check how rack status is collected",
        steps: ["Scroll below the rack table.", "Read the worker status panel for the backend mode and note."],
      },
    ],
    related: ["singleRack", "rackTable"],
    keywords: ["worker", "status", "backend", "mode", "native"],
    aria: {
      label: "Worker status panel",
      description: "Shows the backend worker mode, rack count, and a status note beneath the rack table.",
    },
    aiContext:
      "WorkerStatusPanel shows the native worker mode, rack count, and a backend note beneath the rack table (how status is being collected).",
  },
  {
    id: "statTiles",
    kind: "component",
    title: "Stat tiles",
    domain: "Compute",
    summary: "Metric cards for cluster overview counts and labels.",
    description:
      "StatTile and InfoTile are the metric cards on the cluster overview. StatTile shows a numeric metric (e.g. total GPUs) and InfoTile shows a labelled value (e.g. platform).",
    features: ["Numeric metric cards (StatTile).", "Labelled value cards (InfoTile)."],
    howTo: [
      { task: "Read a metric", steps: ["On the overview, locate the tile.", "Read its number/label."] },
    ],
    related: ["dashboard"],
    keywords: ["stat", "tile", "metric", "card", "count", "info"],
    aria: {
      label: "Cluster metric tiles",
      description: "Metric cards on the cluster overview showing counts and labelled values.",
    },
    aiContext:
      "StatTile/InfoTile are the metric cards on the cluster overview (numeric counts like total GPUs, and labelled values like platform).",
  },
  {
    id: "clusterSqueueOverview",
    kind: "component",
    title: "Cluster squeue overview",
    domain: "Compute",
    summary: "Parsed cluster-wide squeue summary on the overview page.",
    description:
      "ClusterSqueueOverview parses cluster squeue output into a summary table on the overview page, giving a cluster-wide view of running and pending jobs without opening the Scheduler page.",
    features: ["Cluster-wide squeue summary.", "Running/pending job counts at a glance."],
    howTo: [
      { task: "See cluster job activity", steps: ["Open the cluster overview.", "Read the squeue overview table."] },
    ],
    related: ["dashboard", "slurm", "squeuePanel"],
    keywords: ["squeue", "overview", "cluster", "jobs", "summary"],
    aria: {
      label: "Cluster squeue overview",
      description: "Summary table of cluster-wide running and pending Slurm jobs on the overview page.",
    },
    aiContext:
      "ClusterSqueueOverview is a parsed cluster-wide squeue summary table on the overview page (running/pending jobs across the cluster).",
  },
  {
    id: "squeuePanel",
    kind: "component",
    title: "Squeue panel",
    domain: "Compute",
    summary: "Live squeue table with per-job log tail actions.",
    description:
      "SqueuePanel renders a live squeue table of jobs with actions to tail a job's log. It is used both on the Scheduler page and inside the toolbar squeue dialog.",
    features: ["Live job table (squeue).", "Tail a job's log from its row."],
    howTo: [
      { task: "Tail a job log", steps: ["Find the job row.", "Use the tail/log action to stream its output."] },
    ],
    related: ["slurm", "squeueDialog", "sinfoPanel"],
    keywords: ["squeue", "jobs", "queue", "panel", "log", "tail"],
    aria: {
      label: "Squeue job panel",
      description: "Live table of Slurm jobs with the ability to tail a selected job's log.",
    },
    aiContext:
      "SqueuePanel is the live squeue job table (running/pending) with per-job log tailing; used on the Scheduler page and in the toolbar squeue dialog.",
  },
  {
    id: "sinfoPanel",
    kind: "component",
    title: "Sinfo panel",
    domain: "Compute",
    summary: "Live sinfo table of node states and reasons.",
    description:
      "SinfoPanel renders a live `sinfo -R --long` table of node states (idle, alloc, drain, down) and the reasons nodes are unavailable.",
    features: ["Live node-state table.", "Drain/down reasons per node."],
    howTo: [
      { task: "Find unavailable nodes", steps: ["Open the sinfo panel.", "Look for drain/down states and read the reason."] },
    ],
    related: ["slurm", "sinfoDialog", "squeuePanel"],
    keywords: ["sinfo", "nodes", "state", "drain", "down", "idle", "alloc", "reason"],
    aria: {
      label: "Sinfo node panel",
      description: "Live table of Slurm node states and reasons (idle, alloc, drain, down).",
    },
    aiContext:
      "SinfoPanel is the live `sinfo -R --long` table of node states (idle/alloc/drain/down) and drain/down reasons.",
  },
  {
    id: "squeueDialog",
    kind: "dialog",
    title: "Squeue dialog",
    domain: "Compute",
    summary: "Toolbar modal wrapping the squeue panel.",
    description:
      "SqueueDialog is a modal opened from the toolbar that wraps SqueuePanel, letting you check the job queue from anywhere in the app.",
    features: ["Open the squeue panel from the toolbar.", "Same job table and log tailing as the Scheduler page."],
    howTo: [
      { task: "Open the squeue dialog", steps: ["Click the squeue action in the toolbar.", "Review jobs in the modal."] },
    ],
    related: ["squeuePanel", "slurm", "toolbar"],
    keywords: ["squeue", "dialog", "modal", "toolbar", "jobs"],
    aria: {
      label: "Squeue dialog",
      description: "Modal showing the Slurm job queue, opened from the toolbar.",
    },
    aiContext: "SqueueDialog is a toolbar modal wrapping SqueuePanel so users can check the job queue from anywhere.",
  },
  {
    id: "sinfoDialog",
    kind: "dialog",
    title: "Sinfo dialog",
    domain: "Compute",
    summary: "Toolbar modal wrapping the sinfo panel.",
    description:
      "SinfoDialog is a modal opened from the toolbar that wraps SinfoPanel, letting you check node states from anywhere in the app.",
    features: ["Open the sinfo panel from the toolbar.", "Same node-state table as the Scheduler page."],
    howTo: [
      { task: "Open the sinfo dialog", steps: ["Click the sinfo action in the toolbar.", "Review node states in the modal."] },
    ],
    related: ["sinfoPanel", "slurm", "toolbar"],
    keywords: ["sinfo", "dialog", "modal", "toolbar", "nodes"],
    aria: {
      label: "Sinfo dialog",
      description: "Modal showing Slurm node states, opened from the toolbar.",
    },
    aiContext: "SinfoDialog is a toolbar modal wrapping SinfoPanel so users can check node states from anywhere.",
  },
];
