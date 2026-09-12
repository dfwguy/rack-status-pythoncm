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
 * Knowledge entries for the per-rack detail view and the action cards, tabs,
 * node selector, and dialogs that operators use to validate and remediate a
 * single rack.
 */
import type { HelpContent } from "../types";

export const rackDetailContent: HelpContent[] = [
  {
    id: "rackDetail",
    kind: "view",
    title: "Rack detail",
    domain: "Compute",
    routePath: "/compute/racks/:rackId",
    summary: "Drill-down for one rack: status, nodes, and validation/hardware actions.",
    description:
      "The Rack detail view is where you operate on a single rack. It opens with an overview card (validation tag, sinfo, last job, comment), a node selector to scope actions to specific nodes, and tabs that group the available commands: Validation, Hardware, Networking, and Services. Most actions run via an ActionCard with a confirmation and stream live output.",
    features: [
      "Overview card: validation state, sinfo, last job, comment, key fields.",
      "Node selector: multi-select nodes/trays, per-node terminal, issue-count trend and full in-SPA history.",
      "Validation tab: NEMO, burn-in, NCCL/parser runs for selected nodes.",
      "Hardware tab: repeatable deep audit with automatic Slurm comment/drain, plus hardware checks, power reset and MAC change.",
      "Networking tab: repeatable forced-NVLink/IB deep audit with automatic Slurm comment/drain; Services holds service commands.",
      "Live streaming output, last-output indicator, and rack lock awareness.",
    ],
    howTo: [
      {
        task: "Run a validation on specific nodes",
        steps: [
          "Open a rack from the Racks table.",
          "In the node selector, select the target nodes.",
          "Switch to the Validation tab and pick the action card (e.g. NEMO).",
          "Confirm in the dialog; watch the streamed output.",
        ],
      },
      {
        task: "Power-cycle / reset hardware on a node",
        steps: [
          "Select the node(s) in the node selector.",
          "Open the Hardware tab.",
          "Use the power reset / hardware action card and confirm.",
        ],
      },
      {
        task: "Open a web terminal to a node",
        steps: ["Select the node.", "Use the per-node terminal action to open a web terminal session."],
      },
      {
        task: "Run a deep audit and review node history",
        steps: [
          "Select the target nodes and open Hardware or Networking.",
          "Click Start Tests on the deep-audit card; confirmed failures are commented and drained in Slurm.",
          "Click Results beside Start Tests for the per-node audit result and corrective action.",
          "Click ▲n in the node selector to see validation participation, evidence-attributed failures, and detailed Greenlight findings.",
        ],
      },
    ],
    faqs: [
      {
        q: "Why am I warned that the rack is locked?",
        a: "Another operator holds an advisory lock on the rack. Coordinate before running commands; the lock conflict dialog shows who holds it.",
      },
    ],
    tips: ["Scope actions to selected nodes to avoid acting on the whole rack unintentionally."],
    related: [
      "singleRack",
      "rackOverview",
      "nodeSelector",
      "validationTab",
      "hardwareTab",
      "networkingTab",
      "servicesTab",
      "actionCard",
      "lockConflictDialog",
    ],
    keywords: ["rack", "detail", "nodes", "validation", "hardware", "networking", "services", "actions", "drill-down"],
    aria: {
      label: "Rack detail page",
      description:
        "Per-rack workspace with an overview card, a node selector, and Validation, Hardware, Networking, and Services action tabs.",
    },
    aiContext:
      "The Rack detail view operates on one rack. Its node selector adds a validation-history ▲ issue trend; clicking it opens evidence-attributed run and finding history without leaving the SPA. Hardware Deep Audit and NVLink / Network Deep Audit are repeatable durable workflows with Results next to Start Tests. Only structured, confirmed node failures are automatically recorded, given a specific Slurm reason containing the workflow id, and drained; warnings/skips/transport failures remain inconclusive.",
  },
  {
    id: "rackOverview",
    kind: "component",
    title: "Rack overview card",
    domain: "Compute",
    summary: "Summary card of a rack's validation state and key fields.",
    description:
      "RackOverview is the summary card at the top of the rack detail view: validation tag, sinfo state, last job, the rack comment, and key rack fields.",
    features: ["Validation tag and sinfo state.", "Last job and rack comment.", "Key rack fields at a glance."],
    howTo: [
      { task: "Read a rack's current state", steps: ["Open the rack detail view.", "Read the overview card at the top."] },
    ],
    related: ["rackDetail"],
    keywords: ["overview", "card", "summary", "validation", "sinfo", "comment"],
    aria: { label: "Rack overview card", description: "Summary card showing a rack's validation state, sinfo, last job, and comment." },
    aiContext: "RackOverview is the rack detail summary card: validation tag, sinfo state, last job, comment, and key rack fields.",
  },
  {
    id: "nodeSelector",
    kind: "component",
    title: "Node selector",
    domain: "Compute",
    summary: "Tray/node grid with selection, terminal, and clickable issue trends.",
    description:
      "NodeSelector is a grid of the rack's trays/nodes. Select nodes to scope actions, launch a terminal, or click a node's ▲n trend to open validation participation, attributed failures, Slurm state/reason, and detailed local issues in one SPA dialog.",
    features: ["Multi-select nodes/trays.", "Scopes action tabs to the selection.", "Per-node web terminal launch.", "Clickable ▲n / ✖ / ⚑ / ▦ issue flags and full history."],
    howTo: [
      { task: "Scope actions to nodes", steps: ["Click nodes to select them.", "Run actions from the tabs; they apply to the selection."] },
    ],
    related: ["rackDetail", "webTerminalDialog"],
    keywords: ["node", "selector", "tray", "select", "scope", "terminal"],
    aria: { label: "Node selector", description: "Grid of rack nodes with multi-select to scope actions and per-node terminal launch." },
    aiContext: "NodeSelector is the tray/node grid; selecting nodes scopes the action tabs to them, and each node can launch a web terminal.",
  },
  {
    id: "rackLastOutputDot",
    kind: "component",
    title: "Last output indicator",
    domain: "Compute",
    summary: "Indicator/link to the last command output for a rack.",
    description:
      "RackLastOutputDot is a small indicator on the rack header that links to the most recent command output for that rack, so you can quickly reopen the last result.",
    features: ["Shows that recent output exists.", "Links to the last command output."],
    howTo: [
      { task: "Reopen the last command output", steps: ["Look for the last-output dot on the rack header.", "Click it to view the last output."] },
    ],
    related: ["rackDetail", "commandStreamDialog", "commandTextDialog"],
    keywords: ["last", "output", "indicator", "dot", "result"],
    aria: { label: "Last command output indicator", description: "Indicator linking to the most recent command output for the rack." },
    aiContext: "RackLastOutputDot is a rack-header indicator/link to the most recent command output for that rack.",
  },
  {
    id: "validationTab",
    kind: "component",
    title: "Validation tab",
    domain: "Compute",
    summary: "Validation actions (NEMO, burn-in, parser) for selected nodes.",
    description:
      "The Validation tab groups validation action cards — NEMO framework runs, burn-in, NCCL and parser-driven checks — that run against the nodes selected in the node selector.",
    features: ["NEMO and burn-in runs.", "NCCL / parser validation.", "Scoped to selected nodes."],
    howTo: [
      { task: "Run NEMO validation", steps: ["Select nodes.", "Open the Validation tab.", "Use the NEMO action card and confirm."] },
    ],
    related: ["rackDetail", "actionCard", "parserResultsDialog"],
    keywords: ["validation", "nemo", "burn-in", "nccl", "parser", "tab"],
    aria: { label: "Validation actions tab", description: "Validation action cards (NEMO, burn-in, NCCL/parser) for the selected nodes." },
    aiContext: "The Validation tab holds validation action cards (NEMO, burn-in, NCCL/parser) that run on the selected nodes.",
  },
  {
    id: "hardwareTab",
    kind: "component",
    title: "Hardware tab",
    domain: "Compute",
    summary: "Deep hardware audit with automatic Slurm drain, plus hardware actions.",
    description:
      "The Hardware tab starts with a repeatable GB300 deep audit of GPU inventory/thermal/ECC/recovery, Xid/PCIe/EDAC, services, IMEX and storage. Results is beside Start Tests. A structured confirmed failure is recorded and immediately commented/drained in Slurm; warnings and unavailable probes do not drain. Power, MAC, hw-check and other cards remain below it.",
    features: ["Hardware deep audit with per-node durable results.", "Automatic specific Slurm comment and drain for confirmed failures.", "Power reset/cycle.", "MAC change.", "Hardware-checker preset runs."],
    howTo: [
      { task: "Run a hardware check", steps: ["Select nodes.", "Open the Hardware tab.", "Use the hardware check / hw-check card and confirm."] },
    ],
    related: ["rackDetail", "actionCard", "macChangeDialog", "hwCheckRunDialog"],
    keywords: ["hardware", "power", "reset", "mac", "hw-check", "checker", "tab"],
    aria: { label: "Hardware actions tab", description: "Hardware action cards: checks, power reset, MAC change, and hardware-checker runs." },
    aiContext: "The Hardware tab holds hardware action cards (hardware checks, power reset, MAC change, hw-check preset runs) for the selected nodes.",
  },
  {
    id: "networkingTab",
    kind: "component",
    title: "Networking tab",
    domain: "Compute",
    summary: "Forced-NVLink/IB deep audit with automatic Slurm drain, plus network actions.",
    description: "The Networking tab starts with a repeatable GB300 deep audit covering NVLink fabric and a forced data-path probe, IMEX, IB rails/rates/error counters and mlx5 bonding. Results is beside Start Tests. Confirmed node failures are recorded and immediately commented/drained in Slurm; skipped probes remain explicit and do not count as a pass or trigger a drain.",
    features: ["Forced-NVLink and RoCE-control data-path gate.", "IB rail, line-rate, counter and mlx5 bond validation.", "Automatic specific Slurm comment and drain for confirmed node failures.", "Other NVSwitch/NVLMapper network actions."],
    howTo: [
      { task: "Run a network command", steps: ["Select nodes.", "Open the Networking tab.", "Use the relevant action card and confirm."] },
    ],
    related: ["rackDetail", "actionCard"],
    keywords: ["networking", "network", "commands", "tab"],
    aria: { label: "Networking actions tab", description: "Network-related action cards for the selected nodes." },
    aiContext: "The Networking tab holds network-related action cards for the selected nodes.",
  },
  {
    id: "servicesTab",
    kind: "component",
    title: "Services tab",
    domain: "Compute",
    summary: "Service and diagnostic commands for selected nodes.",
    description: "The Services tab groups service/diagnostic action cards that run against the selected nodes.",
    features: ["Service/diagnostic commands.", "Scoped to selected nodes."],
    howTo: [
      { task: "Run a service command", steps: ["Select nodes.", "Open the Services tab.", "Use the relevant action card and confirm."] },
    ],
    related: ["rackDetail", "actionCard"],
    keywords: ["services", "service", "diagnostic", "commands", "tab"],
    aria: { label: "Services actions tab", description: "Service and diagnostic action cards for the selected nodes." },
    aiContext: "The Services tab holds service/diagnostic action cards for the selected nodes.",
  },
  {
    id: "actionCard",
    kind: "component",
    title: "Action card",
    domain: "Compute",
    summary: "Reusable run-action card with an Info panel, confirm dialog, and status.",
    description:
      "ActionCard is the reusable building block for rack commands. Each card carries a category badge, artwork for that category, the action's name and a one-line summary, and two buttons: Info, which opens the full description, and the action itself, which confirms before running. Clicking anywhere on the card opens Info — never the action — so a mis-click on a destructive control such as Drain or BMC Recovery costs nothing. Where nodes are selected, the confirmation lists every hostname before anything runs. A status tag reports Running, Done or Error, and the card is highlighted while its run is in flight. The cards share their appearance with the catalog-driven widgets on the Validation board.",
    features: [
      "Info button and card click open the full description; only the action button runs it.",
      "Confirmation lists every selected node by name before running.",
      "Category badge and artwork, matching the tool catalog's categories.",
      "Status tag (Running / Done / Error) and a highlight while in flight.",
      "Destructive actions are styled in the danger palette and say so in the confirmation.",
      "Streams the command result.",
    ],
    howTo: [
      {
        task: "Read what an action does before running it",
        steps: ["Click the card, or its Info button.", "Read the full description.", "Close the panel."],
      },
      {
        task: "Run an action",
        steps: [
          "Select nodes if the action is node-scoped.",
          "Press the action's own button (Run, Drain, Reset BMC, ...).",
          "Check the node list in the confirmation, then confirm.",
          "Watch the status tag and streamed output.",
        ],
      },
    ],
    related: ["validationTab", "hardwareTab", "networkingTab", "servicesTab", "commandStreamDialog"],
    keywords: ["action", "card", "run", "command", "confirm", "status", "info", "details", "category"],
    aria: { label: "Action card", description: "Run-action card with an Info panel, a confirmation dialog listing the selected nodes, node scope, and status, that streams the command result." },
    aiContext: "ActionCard is the reusable rack-command card: a category badge and artwork, a one-line summary with the full text behind Info, and an action button that confirms (listing every selected node) before running. Clicking the card body opens Info, not the action, because these controls are destructive. It shows a status tag and streams the result, and shares its appearance with the catalog-driven Validation board widgets.",
  },
  {
    id: "commandStreamDialog",
    kind: "dialog",
    title: "Command stream dialog",
    domain: "Compute",
    summary: "Modal streaming live command output.",
    description: "CommandStreamDialog is a modal that streams live stdout/stderr from a running command endpoint.",
    features: ["Live streaming output.", "Auto-scroll as output arrives."],
    howTo: [{ task: "Watch a command run", steps: ["Run an action that streams.", "Read the live output in the dialog."] }],
    related: ["actionCard", "commandTextDialog"],
    keywords: ["command", "stream", "dialog", "output", "live", "stdout"],
    aria: { label: "Command stream dialog", description: "Modal that streams live command output." },
    aiContext: "CommandStreamDialog is a modal that streams live command stdout/stderr as it runs.",
  },
  {
    id: "commandTextDialog",
    kind: "dialog",
    title: "Command text dialog",
    domain: "Compute",
    summary: "Modal showing static command text or JSON result.",
    description: "CommandTextDialog is a modal that shows a completed command's static text output or a JSON result.",
    features: ["Shows static text/JSON output.", "Copyable result."],
    howTo: [{ task: "View a completed result", steps: ["Open the last-output indicator or finish a command.", "Read the result in the dialog."] }],
    related: ["actionCard", "commandStreamDialog"],
    keywords: ["command", "text", "dialog", "output", "json", "result"],
    aria: { label: "Command text dialog", description: "Modal showing a command's static text or JSON result." },
    aiContext: "CommandTextDialog is a modal showing a completed command's static text output or JSON result.",
  },
  {
    id: "webTerminalDialog",
    kind: "dialog",
    title: "Web terminal dialog",
    domain: "Compute",
    summary: "Embedded web terminal to a node.",
    description:
      "WebTerminalDialog opens an embedded web terminal session to a node (it opens the /terminal page with session params), so you can run commands interactively.",
    features: ["Interactive terminal in-app.", "Per-node session."],
    howTo: [{ task: "Open a node terminal", steps: ["Select a node in the node selector.", "Use the terminal action to open the web terminal."] }],
    related: ["nodeSelector", "terminal"],
    keywords: ["terminal", "web", "shell", "ssh", "dialog", "node"],
    aria: { label: "Web terminal dialog", description: "Embedded interactive web terminal session to a node." },
    aiContext: "WebTerminalDialog opens an embedded interactive web terminal to a node (loads /terminal with session params).",
  },
  {
    id: "lockConflictDialog",
    kind: "dialog",
    title: "Lock conflict dialog",
    domain: "Compute",
    summary: "Warns when another operator holds a rack lock.",
    description:
      "LockConflictDialog appears when you try to run commands on a rack that another operator has locked (advisory lock). It shows who holds the lock so you can coordinate.",
    features: ["Shows the current lock holder.", "Prevents accidental concurrent operations."],
    howTo: [{ task: "Resolve a lock conflict", steps: ["Read who holds the lock.", "Coordinate with them or wait for the lock to release."] }],
    related: ["rackDetail"],
    keywords: ["lock", "conflict", "rack", "operator", "advisory", "dialog"],
    aria: { label: "Rack lock conflict dialog", description: "Warning shown when another operator holds an advisory lock on the rack." },
    aiContext: "LockConflictDialog warns that another operator holds an advisory rack lock and shows who, so users coordinate before running commands.",
  },
  {
    id: "macChangeDialog",
    kind: "dialog",
    title: "MAC change dialog",
    domain: "Compute",
    summary: "Edit device/name MAC mappings for a node.",
    description: "MacChangeDialog lets you edit the device/name to MAC address mappings for a node.",
    features: ["Edit MAC mappings per device.", "Apply changes to a node."],
    howTo: [{ task: "Change a node MAC", steps: ["Open the Hardware tab and the MAC change action.", "Edit the mapping and apply."] }],
    related: ["hardwareTab"],
    keywords: ["mac", "address", "change", "device", "dialog", "node"],
    aria: { label: "MAC change dialog", description: "Editor for a node's device-to-MAC address mappings." },
    aiContext: "MacChangeDialog edits a node's device/name-to-MAC address mappings.",
  },
  {
    id: "hwCheckRunDialog",
    kind: "dialog",
    title: "Hardware checker dialog",
    domain: "Compute",
    summary: "Configure and run hardware-checker presets.",
    description:
      "HwCheckRunDialog configures and runs hardware-checker presets on selected nodes, and can show the checker's `--help` output.",
    features: ["Choose a hardware-checker preset.", "Run on selected nodes.", "View checker.sh --help."],
    howTo: [{ task: "Run a hardware checker", steps: ["Open the Hardware tab.", "Open the hw-check dialog, pick a preset, and run."] }],
    related: ["hardwareTab"],
    keywords: ["hardware", "checker", "hw-check", "preset", "dialog", "run"],
    aria: { label: "Hardware checker dialog", description: "Configure and run hardware-checker presets on selected nodes." },
    aiContext: "HwCheckRunDialog configures and runs hardware-checker presets on selected nodes (optionally showing checker.sh --help).",
  },
];
