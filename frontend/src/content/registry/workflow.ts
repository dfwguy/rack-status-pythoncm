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
 * Knowledge entries for the Workflows view and the workflow editor (mermaid +
 * React Flow), run dialogs, and the AI workflow generator.
 */
import type { HelpContent } from "../types";

export const workflowContent: HelpContent[] = [
  {
    id: "workflows",
    kind: "view",
    title: "Workflows",
    domain: "Tools",
    routePath: "/tools/workflows",
    summary: "Design, save, and run multi-step rack workflows.",
    description:
      "The Workflows page lets you build reusable multi-step procedures (e.g. an energise-and-validate runbook) as a flowchart, save them, and execute them against racks while tracking step-by-step progress. Mermaid source text is the single source of truth and is kept in sync with a React Flow canvas.",
    features: [
      "Visual workflow editor (React Flow canvas) synced to mermaid source.",
      "Node types: automated (green), manual/event (blue), decision (yellow).",
      "Save workflows and view past runs.",
      "Run a workflow against a rack/target with live progress.",
      "AI-assisted workflow generation from a natural-language prompt.",
    ],
    howTo: [
      {
        task: "Create and run a workflow",
        steps: [
          "Open Tools > Workflows and create a new workflow.",
          "Build the flow in the editor (or generate it with AI), then save.",
          "Start a run, choose the target rack/hostlist, and watch step progress.",
        ],
      },
    ],
    tips: ["Mermaid text is canonical; the canvas reads from and writes back to it. Node shape determines step type, so keep shapes intact when editing."],
    related: ["workflowList", "workflowDesigner", "flowDiagram", "workflowRunDialog", "workflowAIGenerator"],
    keywords: ["workflow", "workflows", "runbook", "flowchart", "mermaid", "react flow", "automation", "steps"],
    aria: { label: "Workflows page", description: "Design, save, and run multi-step rack workflows with a visual editor synced to mermaid source." },
    aiContext:
      "The Workflows page builds reusable multi-step rack procedures as flowcharts (mermaid is the source of truth, synced to a React Flow canvas) and runs them against racks with live step progress. Node types: automated (green rect), manual/event (blue stadium), decision (yellow diamond). Includes AI-assisted generation.",
  },
  {
    id: "workflowList",
    kind: "component",
    title: "Workflow list",
    domain: "Tools",
    summary: "Lists saved workflows and past runs.",
    description: "WorkflowList lists saved workflows and previous runs with actions to create, edit, run, and delete.",
    features: ["List of saved workflows.", "Past run history.", "Create / edit / run / delete actions."],
    howTo: [{ task: "Run a saved workflow", steps: ["Find the workflow in the list.", "Click run and choose the target."] }],
    related: ["workflows", "workflowDesigner", "workflowRunDialog"],
    keywords: ["workflow", "list", "saved", "runs", "history"],
    aria: { label: "Workflow list", description: "List of saved workflows and past runs with create, edit, run, and delete actions." },
    aiContext: "WorkflowList lists saved workflows and past runs with create/edit/run/delete actions.",
  },
  {
    id: "workflowDesigner",
    kind: "component",
    title: "Workflow designer",
    domain: "Tools",
    summary: "Mermaid + React Flow editor; save workflow definition.",
    description:
      "WorkflowDesigner is the editor: a code panel of mermaid source alongside the React Flow canvas. Edits in either stay in sync; saving preserves node step types inferred from mermaid shapes.",
    features: ["Mermaid code panel + canvas.", "Two-way sync.", "Save preserves step types."],
    howTo: [{ task: "Edit a workflow", steps: ["Open a workflow in the designer.", "Edit the mermaid or the canvas.", "Save to persist."] }],
    related: ["workflows", "flowDiagram", "customNode"],
    keywords: ["designer", "editor", "mermaid", "canvas", "react flow", "save"],
    aria: { label: "Workflow designer", description: "Workflow editor with a mermaid code panel synced to a React Flow canvas." },
    aiContext: "WorkflowDesigner is the workflow editor: mermaid code panel + React Flow canvas kept in sync; saving preserves step types inferred from mermaid shapes.",
  },
  {
    id: "flowDiagram",
    kind: "component",
    title: "Flow diagram canvas",
    domain: "Tools",
    summary: "React Flow canvas: nodes, edges, connections, toolbar.",
    description:
      "FlowDiagram is the React Flow canvas wrapper. It registers node/edge types, handles connections (Top/Left = target, Bottom/Right = source), and provides the editor toolbar; it syncs back to mermaid.",
    features: ["Node/edge rendering.", "Connection handles.", "Editor toolbar.", "Mermaid sync."],
    howTo: [{ task: "Connect two nodes", steps: ["Drag from a node's bottom/right handle.", "Drop on another node's top/left handle."] }],
    related: ["workflowDesigner", "customNode", "customEdge"],
    keywords: ["flow", "diagram", "canvas", "nodes", "edges", "connect", "react flow"],
    aria: { label: "Workflow canvas", description: "React Flow canvas for arranging workflow nodes and edges." },
    aiContext: "FlowDiagram is the React Flow canvas wrapper (node/edge types, connection handles, toolbar) that syncs back to mermaid.",
  },
  {
    id: "customNode",
    kind: "component",
    title: "Workflow node",
    domain: "Tools",
    summary: "Automated/manual/decision node renderers with edge-delete on hover.",
    description:
      "CustomNode renders the workflow node styles: automated (green rectangle), manual/event (blue stadium), and decision (yellow diamond). Edge delete buttons appear on node hover.",
    features: ["Three node styles by step type.", "Edge delete buttons on hover."],
    howTo: [{ task: "Delete an edge", steps: ["Hover the node.", "Click the edge delete button that appears."] }],
    related: ["flowDiagram", "customEdge", "nodePropertiesDialog"],
    keywords: ["node", "automated", "manual", "decision", "diamond", "stadium", "rectangle"],
    aria: { label: "Workflow node", description: "Workflow node rendered by step type (automated, manual/event, or decision)." },
    aiContext: "CustomNode renders workflow nodes by step type: automated (green rect), manual/event (blue stadium), decision (yellow diamond); edge delete buttons appear on hover.",
  },
  {
    id: "customEdge",
    kind: "component",
    title: "Workflow edge",
    domain: "Tools",
    summary: "Smooth-step edges with optional labels.",
    description: "CustomEdge renders workflow connections as smooth-step edges with optional labels.",
    features: ["Smooth-step edge rendering.", "Optional edge labels."],
    howTo: [{ task: "Label an edge", steps: ["In mermaid, add a label to the connection.", "The canvas renders it on the edge."] }],
    related: ["flowDiagram", "customNode"],
    keywords: ["edge", "connection", "smoothstep", "label", "arrow"],
    aria: { label: "Workflow edge", description: "Smooth-step connection between two workflow nodes, with an optional label." },
    aiContext: "CustomEdge renders workflow connections as smooth-step edges with optional labels.",
  },
  {
    id: "nodePropertiesDialog",
    kind: "dialog",
    title: "Node properties dialog",
    domain: "Tools",
    summary: "Edit node name, command/tool, timeouts, and step type.",
    description: "NodePropertiesDialog edits a workflow node's name, command/tool, timeouts, and step type.",
    features: ["Edit node name and step type.", "Set command/tool and timeouts."],
    howTo: [{ task: "Configure a node", steps: ["Open the node in the designer.", "Edit its properties and save."] }],
    related: ["customNode", "workflowDesigner"],
    keywords: ["node", "properties", "dialog", "command", "timeout", "step type"],
    aria: { label: "Node properties dialog", description: "Editor for a workflow node's name, command/tool, timeouts, and step type." },
    aiContext: "NodePropertiesDialog edits a workflow node's name, command/tool, timeouts, and step type.",
  },
  {
    id: "workflowRunDialog",
    kind: "dialog",
    title: "Workflow run dialog",
    domain: "Tools",
    summary: "Start a run with rack/target/hostlist overrides.",
    description: "WorkflowRunDialog starts a workflow run, letting you set the target rack, target, or hostlist overrides.",
    features: ["Choose target rack/hostlist.", "Start a run."],
    howTo: [{ task: "Start a run", steps: ["Click run on a workflow.", "Set the rack/target/hostlist.", "Start."] }],
    related: ["workflows", "workflowRunPanel", "workflowList"],
    keywords: ["workflow", "run", "dialog", "target", "rack", "hostlist"],
    aria: { label: "Workflow run dialog", description: "Start a workflow run with rack, target, or hostlist overrides." },
    aiContext: "WorkflowRunDialog starts a workflow run with target rack/target/hostlist overrides.",
  },
  {
    id: "workflowRunPanel",
    kind: "component",
    title: "Workflow run panel",
    domain: "Tools",
    summary: "Side panel for active run progress and step actions.",
    description: "WorkflowRunPanel is a side panel that shows an active run's progress and per-step actions.",
    features: ["Live run progress.", "Per-step actions."],
    howTo: [{ task: "Monitor a run", steps: ["Start a run.", "Watch progress in the run panel and act on steps."] }],
    related: ["workflowRunDialog", "stepOutputDialog"],
    keywords: ["workflow", "run", "panel", "progress", "steps"],
    aria: { label: "Workflow run panel", description: "Side panel showing an active workflow run's progress and step actions." },
    aiContext: "WorkflowRunPanel is a side panel showing active run progress and per-step actions.",
  },
  {
    id: "stepOutputDialog",
    kind: "dialog",
    title: "Step output dialog",
    domain: "Tools",
    summary: "View output from a completed workflow step.",
    description: "StepOutputDialog shows the output of a completed workflow step.",
    features: ["View completed step output."],
    howTo: [{ task: "Read step output", steps: ["In the run panel, click a completed step.", "Read its output in the dialog."] }],
    related: ["workflowRunPanel"],
    keywords: ["step", "output", "dialog", "result"],
    aria: { label: "Step output dialog", description: "Shows the output of a completed workflow step." },
    aiContext: "StepOutputDialog shows the output of a completed workflow step.",
  },
  {
    id: "workflowAIGenerator",
    kind: "dialog",
    title: "AI workflow generator",
    domain: "Tools",
    summary: "Generate a workflow flowchart from a natural-language prompt.",
    description:
      "WorkflowAIGenerator is a dialog that turns a natural-language description into a Mermaid flowchart using the AI assistant, which you can then refine in the designer.",
    features: ["Natural-language to mermaid flowchart.", "Refine the result in the editor."],
    howTo: [{ task: "Generate a workflow with AI", steps: ["Open the AI generator.", "Describe the workflow.", "Generate, then refine in the designer and save."] }],
    related: ["workflows", "workflowDesigner", "aiChatWidget"],
    keywords: ["ai", "generator", "workflow", "mermaid", "prompt", "generate"],
    aria: { label: "AI workflow generator", description: "Generate a workflow flowchart from a natural-language description using AI." },
    aiContext: "WorkflowAIGenerator turns a natural-language description into a Mermaid workflow flowchart via the AI assistant, for refinement in the designer.",
  },
];
