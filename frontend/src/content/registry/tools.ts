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
    summary: "Web terminal preview that links to the full terminal page.",
    description:
      "The Terminal page is a preview that links to the standalone full-page web terminal (served at /terminal). Use it to open an interactive shell session to the head node or a selected node.",
    features: ["Preview/link to the full web terminal.", "Interactive shell session."],
    howTo: [{ task: "Open the web terminal", steps: ["Open Tools > Terminal.", "Follow the link to open the full /terminal page."] }],
    related: ["webTerminalDialog", "nodeSelector"],
    keywords: ["terminal", "shell", "ssh", "console", "preview"],
    aria: { label: "Terminal page", description: "Preview that links to the standalone full-page web terminal." },
    aiContext: "The Terminal page is a preview/link to the standalone full-page web terminal (/terminal) for an interactive shell session.",
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
