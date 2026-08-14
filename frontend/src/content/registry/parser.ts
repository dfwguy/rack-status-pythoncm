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
 * Knowledge entries for parser results, benchmark charts, and log dialogs that
 * are shared across the racks, multirack, and tools views.
 */
import type { HelpContent } from "../types";

export const parserContent: HelpContent[] = [
  {
    id: "parserResultsDialog",
    kind: "dialog",
    title: "Parser results",
    domain: "Compute",
    summary: "NCCL/NEMO parser charts, log picker, Clusterkit, nvloom, and Grafana.",
    description:
      "ParserResultsDialog is the large analysis dialog opened from a rack or the table. It parses validation logs into charts (NCCL bandwidth, NEMO metrics), lets you pick which log to parse, and includes Clusterkit results, nvloom heatmaps, and embedded Grafana panels across tabs.",
    features: [
      "NCCL/NEMO parsed charts from validation logs.",
      "Log picker to choose which log to parse.",
      "Clusterkit results section.",
      "nvloom heatmaps.",
      "Embedded Grafana panels.",
    ],
    howTo: [
      {
        task: "View NCCL bandwidth for a rack",
        steps: [
          "Open the parser results from the rack row menu or detail view.",
          "Pick the NCCL log in the log picker.",
          "Read the parsed bandwidth chart.",
        ],
      },
    ],
    tips: ["If charts are empty, confirm a parseable log exists in the log directory for that rack."],
    related: ["singleRack", "rackDetail", "parserGrafanaEmbeds", "nvloomHeatmapsSection", "clusterkitResultsSection"],
    keywords: ["parser", "results", "nccl", "nemo", "charts", "benchmark", "grafana", "clusterkit", "nvloom"],
    aria: {
      label: "Parser results dialog",
      description: "Analysis dialog with NCCL/NEMO charts, a log picker, Clusterkit results, nvloom heatmaps, and Grafana panels.",
    },
    aiContext:
      "ParserResultsDialog parses validation logs into NCCL/NEMO charts and includes a log picker, Clusterkit results, nvloom heatmaps, and embedded Grafana panels. Opened from a rack row or detail view to analyse benchmark output.",
  },
  {
    id: "parserGrafanaEmbeds",
    kind: "component",
    title: "Grafana embeds",
    domain: "Compute",
    summary: "Embedded Grafana panels inside parser results.",
    description: "ParserGrafanaEmbeds renders Grafana panel iframes inside the parser results dialog for live telemetry alongside parsed charts.",
    features: ["Embedded Grafana panels.", "Live telemetry next to parsed results."],
    howTo: [{ task: "View Grafana telemetry", steps: ["Open parser results.", "Switch to the Grafana tab to see embedded panels."] }],
    related: ["parserResultsDialog"],
    keywords: ["grafana", "embed", "iframe", "telemetry", "panel"],
    aria: { label: "Grafana embeds", description: "Embedded Grafana telemetry panels shown inside the parser results dialog." },
    aiContext: "ParserGrafanaEmbeds shows embedded Grafana panel iframes inside the parser results dialog.",
  },
  {
    id: "nvloomHeatmapsSection",
    kind: "component",
    title: "nvloom heatmaps",
    domain: "Compute",
    summary: "nvloom heatmap visualization from log content.",
    description: "NvloomHeatmapsSection renders nvloom heatmaps parsed from log content, visualising bandwidth/latency across the fabric.",
    features: ["Heatmap visualization of nvloom results.", "Parsed from log content."],
    howTo: [{ task: "View nvloom heatmaps", steps: ["Open parser results.", "Open the nvloom section to view the heatmaps."] }],
    related: ["parserResultsDialog", "toolsLiveOutputDialog"],
    keywords: ["nvloom", "heatmap", "bandwidth", "fabric", "visualization"],
    aria: { label: "nvloom heatmaps", description: "Heatmap visualization of nvloom bandwidth/latency results parsed from logs." },
    aiContext: "NvloomHeatmapsSection renders nvloom heatmaps (bandwidth/latency across the fabric) parsed from log content.",
  },
  {
    id: "clusterkitResultsSection",
    kind: "component",
    title: "Clusterkit results",
    domain: "Compute",
    summary: "Clusterkit run list and results inside the parser dialog.",
    description: "ClusterkitResultsSection lists Clusterkit runs and displays their results inside the parser dialog.",
    features: ["List of Clusterkit runs.", "Result display per run."],
    howTo: [{ task: "Review Clusterkit results", steps: ["Open parser results.", "Open the Clusterkit section and pick a run."] }],
    related: ["parserResultsDialog", "tools"],
    keywords: ["clusterkit", "results", "runs", "benchmark"],
    aria: { label: "Clusterkit results", description: "List of Clusterkit runs and their results inside the parser dialog." },
    aiContext: "ClusterkitResultsSection lists Clusterkit runs and shows their results inside the parser dialog.",
  },
  {
    id: "tailLogDialog",
    kind: "dialog",
    title: "Tail log dialog",
    domain: "Compute",
    summary: "Stream/tail validation or multirack logs.",
    description: "TailLogDialog streams (tails) a validation or multirack log file in a modal so you can watch progress live.",
    features: ["Live tail of a log file.", "Auto-scroll as lines arrive."],
    howTo: [{ task: "Tail a validation log", steps: ["From a rack or multirack run, open the tail log action.", "Watch the log stream."] }],
    related: ["singleRack", "multirack", "commandStreamDialog"],
    keywords: ["tail", "log", "dialog", "stream", "validation", "multirack"],
    aria: { label: "Tail log dialog", description: "Modal that streams (tails) a validation or multirack log file." },
    aiContext: "TailLogDialog streams (tails) a validation or multirack log file in a modal for live monitoring.",
  },
];
