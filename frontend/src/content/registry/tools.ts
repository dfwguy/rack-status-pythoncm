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
 * Knowledge entries for the Tools domain: operations & scripts, the terminal
 * pages, and the live tool output dialog.
 */
import type { HelpContent } from "../types";

export const toolsContent: HelpContent[] = [
  {
    id: "tools",
    kind: "view",
    title: "Operations & Scripts",
    domain: "Tools",
    routePath: "/tools/operations",
    summary: "Run utility scripts and installers: NEMO, HPC-X, CUDA/NCCL, nvloom, Grafana.",
    description:
      "The Operations & Scripts page is the utility toolbox. It runs install and benchmark scripts on the cluster — NEMO, HPC-X, CUDA/NCCL, nvloom, Grafana install — and streams their stdout live. Clusterkit and NVLoom workspace tooling is embedded here.",
    features: [
      "Run installer/benchmark scripts with live streamed output.",
      "NEMO, HPC-X, CUDA/NCCL, nvloom, and Grafana install helpers.",
      "Clusterkit and NVLoom workspace tooling.",
    ],
    howTo: [
      {
        task: "Run a utility script",
        steps: ["Open Tools > Operations & Scripts.", "Pick the script/installer.", "Start it and watch the live output dialog."],
      },
    ],
    related: ["toolsLiveOutputDialog", "clusterkitResultsSection", "nvloomHeatmapsSection", "workflows"],
    keywords: ["tools", "operations", "scripts", "utility", "install", "nemo", "hpc-x", "cuda", "nccl", "nvloom", "grafana", "clusterkit"],
    aria: { label: "Operations and scripts page", description: "Utility toolbox to run install and benchmark scripts (NEMO, HPC-X, CUDA/NCCL, nvloom, Grafana) with live output." },
    aiContext:
      "The Operations & Scripts (tools) page runs utility/install/benchmark scripts on the cluster (NEMO, HPC-X, CUDA/NCCL, nvloom, Grafana install) with live streamed output, plus embedded Clusterkit and NVLoom tooling.",
  },
  {
    id: "terminal",
    kind: "view",
    title: "Terminal",
    domain: "Tools",
    routePath: "/tools/terminal",
    summary: "A grid of live head-node and compute shells, side by side in one workspace.",
    description:
      "The Terminal page is a workspace of live shells. Add terminal opens a searchable picker of every compute node in the active cluster plus the head node; each one you add becomes a pane in a grid that grows from one full-width shell to two side by side, then two by two, and on from there. Panes are dragged by their header (the terminal body stays selectable) and resized from their corner, and the shell reflows to whatever size you give it. A single pane detaches to its own browser tab, or Detach grid moves the whole arrangement to a new tab at once. Detaching hands over the running session rather than dialling a second one, so it is a move, not a duplicate. Your panes and their arrangement are remembered per cluster; the sessions themselves end with the page.",
    features: [
      "Searchable picker over every compute node in the active cluster, plus the head node.",
      "Grid grows 1x1 to 2x1 to 2x2 as you add shells; drag by the header, resize from the corner.",
      "Detach one pane, or the whole grid, into a browser tab — carrying the live session.",
      "Pane list and layout are remembered per cluster across reloads.",
    ],
    howTo: [
      {
        task: "Open shells on several nodes at once",
        steps: [
          "Open Tools > Terminal.",
          "Click Add terminal and pick the head node, or search for a compute node by name or rack.",
          "Repeat to add more; each new shell is placed beside or below the last.",
          "Drag a pane by its header to rearrange, or its corner to resize.",
        ],
      },
      {
        task: "Move the terminals into their own window",
        steps: [
          "Use Detach grid to move every open shell to a new browser tab.",
          "Or use the detach icon on a single pane's header to move just that one.",
        ],
      },
    ],
    related: ["webTerminalDialog", "nodeSelector"],
    keywords: ["terminal", "shell", "ssh", "console", "grid", "multiple terminals", "detach", "head node", "pdsh", "cmsh"],
    aria: { label: "Terminal page", description: "A resizable grid of live head-node and compute shells." },
    aiContext:
      "Tools > Terminal is a react-grid-layout workspace of live xterm.js shells over the dashboard's SSH path. Add terminal opens a picker listing compute nodes (derived from rack rows by parseNodesFromRack) plus the head node (target 'headnode', mode 'headnode'). Panes auto-place 1x1 -> 2x1 -> 2x2 and are draggable by header only so terminal text stays selectable. A pane detaches to /terminal?session=<id> and the whole grid to /terminal-grid?g=<base64url>, both carrying live session ids so shells are adopted rather than reopened. Layout persists per cluster in localStorage; sessions do not survive a reload.",
  },
  {
    id: "toolsLiveOutputDialog",
    kind: "dialog",
    title: "Tool live output dialog",
    domain: "Tools",
    summary: "Streams stdout from utility scripts.",
    description:
      "ToolsLiveOutputDialog streams the live stdout of a utility script (NEMO, HPC-X, CUDA/NCCL, nvloom, Grafana install) so you can watch installs/benchmarks as they run.",
    features: ["Live stdout streaming.", "Auto-scroll as output arrives."],
    howTo: [{ task: "Watch a script run", steps: ["Start a script from the Operations page.", "Read the live output dialog."] }],
    related: ["tools", "commandStreamDialog"],
    keywords: ["tool", "output", "dialog", "stream", "stdout", "script", "live"],
    aria: { label: "Tool live output dialog", description: "Modal that streams the live stdout of a utility script." },
    aiContext: "ToolsLiveOutputDialog streams the live stdout of utility scripts (NEMO, HPC-X, CUDA/NCCL, nvloom, Grafana install).",
  },
];
