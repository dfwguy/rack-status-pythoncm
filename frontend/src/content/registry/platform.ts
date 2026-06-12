/**
 * Knowledge entries for the Platform domain: Settings, the AI configuration
 * panel, and the Help page itself.
 */
import type { HelpContent } from "../types";

export const platformContent: HelpContent[] = [
  {
    id: "projects",
    kind: "view",
    title: "Projects & Clusters",
    domain: "Platform",
    routePath: "/platform/projects",
    summary: "Customer projects and clusters; set the active cluster to re-scope the app.",
    description:
      "Projects & Clusters defines each customer engagement and the clusters within it: naming convention (rack/node/NVSwitch templates), site geometry, the hardware platform, the cluster manager (BCM/MAAS), and the connection (local or SSH). The active cluster is server-authoritative: switching it re-scopes status, validation geometry, tool execution, and the AI context across the whole app.",
    features: [
      "Create projects with a customer name and description.",
      "Add clusters with platform, manager, naming convention, geometry, and connection.",
      "Set the active cluster (also available from the top-bar switcher).",
    ],
    howTo: [
      { task: "Switch the active cluster", steps: ["Open Projects & Clusters.", "Find the cluster card.", "Click 'Set active' (or use the top-bar switcher)."] },
      { task: "Add a cluster", steps: ["Open the project.", "Click 'Cluster'.", "Pick a platform, set naming + connection, and save."] },
    ],
    related: ["platformCatalog", "toolRegistry", "settings"],
    keywords: ["project", "cluster", "customer", "naming", "active", "switch", "tenant", "bcm", "maas", "ssh"],
    aria: { label: "Projects and clusters page", description: "Manage customer projects and clusters and set the active cluster." },
    aiContext:
      "Projects & Clusters manages customer projects and their clusters (platform, manager, naming, geometry, connection). The active cluster is server-authoritative and re-scopes status, geometry, tools, and AI context.",
  },
  {
    id: "platformCatalog",
    kind: "view",
    title: "Platform Catalog",
    domain: "Platform",
    routePath: "/platform/catalog",
    summary: "Hardware/system SKUs (GPU, CPU, vendor, topology, geometry, capabilities).",
    description:
      "The Platform Catalog defines hardware/system SKUs across NVIDIA generations (GB200/GB300/B200/B300, future VR200/R200), with CPU vendor (Grace/Intel/AMD), system vendor (NVIDIA/DGX/Dell/SMC), form factor, per-rack geometry, interconnect, capabilities (nvlink, nvswitch, imex, mnnvl, roce), and tool parameter defaults. A cluster's platform drives dashboard geometry and which tools are compatible.",
    features: [
      "Define GPU/CPU/vendor/form-factor and per-rack geometry.",
      "Declare capabilities used for tool compatibility.",
      "Set tool parameter defaults (e.g. GPU model flag, MNNVL topology, CUDA major).",
    ],
    howTo: [
      { task: "Add a platform", steps: ["Open Platform Catalog.", "Click 'New platform'.", "Fill GPU/CPU/vendor, geometry, capabilities, and save."] },
    ],
    related: ["projects", "toolRegistry"],
    keywords: ["platform", "gpu", "gb200", "gb300", "b200", "b300", "vr200", "r200", "geometry", "capability", "nvlink", "nvswitch", "mnnvl", "vendor"],
    aria: { label: "Platform catalog page", description: "Define hardware/system platforms and their geometry and capabilities." },
    aiContext:
      "The Platform Catalog defines hardware SKUs (GPU/CPU/vendor/form-factor), per-rack geometry, capabilities, and tool param defaults. The active cluster's platform drives geometry and tool compatibility.",
  },
  {
    id: "toolRegistry",
    kind: "view",
    title: "Tool Registry",
    domain: "Platform",
    routePath: "/platform/tools",
    summary: "Each script as a document: platform compatibility, surfaces, and execution.",
    description:
      "The Tool Registry describes each validation/performance/maintenance script as a document: the script basename, optional env-key override, platform compatibility (all platforms, required capabilities, or specific platform ids), where it surfaces in the UI, and how it executes (async/sync/stream; rack/multirack/cluster/node). Tools are filtered to the active cluster's platform so only compatible tools are offered.",
    features: [
      "Register a tool with its script and execution mode/target.",
      "Set platform compatibility by capability or platform id.",
      "Declare UI surfaces (e.g. rackDetail.hardware).",
    ],
    howTo: [
      { task: "Register a tool", steps: ["Open Tool Registry.", "Click 'New tool'.", "Set script, compatibility, surfaces, execution, and save."] },
    ],
    related: ["projects", "platformCatalog", "tools"],
    keywords: ["tool", "registry", "script", "sysinfo", "clusterkit", "nvloom", "compatibility", "surface", "execution", "platform"],
    aria: { label: "Tool registry page", description: "Define tools, their platform compatibility, surfaces, and execution." },
    aiContext:
      "The Tool Registry describes scripts as documents (script, compatibility by capability/platform, surfaces, execution mode/target). Tools are filtered to the active platform.",
  },
  {
    id: "settings",
    kind: "view",
    title: "Settings",
    domain: "Platform",
    routePath: "/settings",
    summary: "Configure the app: log directory, dashboard env, and the AI assistant.",
    description:
      "The Settings page configures the application: the active log directory used for status and parsing, dashboard environment values, and the AI assistant (endpoint, model, API key, and custom system rules).",
    features: [
      "Set the active log directory.",
      "Configure dashboard environment values.",
      "Configure the AI assistant: endpoint, model, API key, custom system rules.",
      "Test the AI connection.",
    ],
    howTo: [
      {
        task: "Configure the AI assistant",
        steps: [
          "Open Settings.",
          "In the AI configuration panel, set the endpoint, model, and API key.",
          "Optionally add custom system rules.",
          "Use Test connection to verify, then save.",
        ],
      },
      { task: "Change the log directory", steps: ["Open Settings.", "Update the log directory field.", "Save; status/parsing use the new directory."] },
    ],
    related: ["aiConfigurationPanel", "aiChatWidget"],
    keywords: ["settings", "configuration", "log directory", "ai", "endpoint", "model", "api key", "environment"],
    aria: { label: "Settings page", description: "Configure the log directory, dashboard environment, and the AI assistant." },
    aiContext:
      "The Settings page configures the log directory, dashboard environment, and the AI assistant (endpoint, model, API key, custom system rules) with a connection test.",
  },
  {
    id: "aiConfigurationPanel",
    kind: "component",
    title: "AI configuration panel",
    domain: "Platform",
    summary: "Endpoint, model, API key, and custom system rules for the assistant.",
    description:
      "AIConfigurationPanel (in Settings) configures the AI assistant: the OpenAI-compatible endpoint, model name, API key, and custom system rules that are appended to the assistant's system prompt. It can test connectivity.",
    features: ["Set endpoint, model, and API key.", "Add custom system rules.", "Test the connection."],
    howTo: [{ task: "Add custom AI rules", steps: ["Open Settings.", "In the AI panel, enter custom system rules.", "Save; the rules are appended to every prompt."] }],
    related: ["settings", "aiChatWidget"],
    keywords: ["ai", "configuration", "endpoint", "model", "api key", "system rules", "settings"],
    aria: { label: "AI configuration panel", description: "Configure the AI assistant's endpoint, model, API key, and custom system rules." },
    aiContext:
      "AIConfigurationPanel (Settings) configures the assistant: endpoint, model, API key, and custom system rules (appended to the system prompt), with a connection test.",
  },
  {
    id: "help",
    kind: "component",
    title: "Help",
    domain: "Platform",
    summary: "Searchable in-app help panel that slides in from the right.",
    description:
      "Help is a right-hand slide-out panel (KUI SidePanel) overlaying the current page so you keep context while you read, then close it. It is generated from the same knowledge registry that powers the AI assistant and accessibility, so every page and component has a searchable entry with a description, capabilities, how-to steps, and FAQs.",
    features: [
      "Slides in from the right as an overlay; close it to return to your work.",
      "Search across all pages and components, grouped by domain.",
      "Per-topic detail with descriptions, features, how-to steps, FAQs, and tips; opened contextually from any page header.",
    ],
    howTo: [
      { task: "Open help for the current page", steps: ["Click the help icon in the page header.", "The panel slides in showing that page's entry.", "Close it when done."] },
      { task: "Search for a feature", steps: ["Open Help from the sidebar.", "Type a keyword in the search box.", "Open the matching topic."] },
    ],
    related: ["aiChatWidget", "sidebar"],
    keywords: ["help", "guide", "documentation", "search", "how-to", "faq", "support", "accessibility", "panel", "overlay"],
    aria: { label: "Help panel", description: "Searchable in-app help panel grouped by domain that slides in from the right." },
    aiContext:
      "Help is a right-hand slide-out panel (KUI SidePanel) generated from the knowledge registry; every page/component has an entry with description, features, how-to steps, and FAQs. Open it from the page header help icon or the sidebar.",
  },
];
