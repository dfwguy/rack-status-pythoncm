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
      "Script is a basename, not a path: it resolves through the project's own copy, then custom/, then the platform layer, then the shared defaults.",
    ],
    howTo: [
      { task: "Register a tool", steps: ["Open Tool Registry.", "Click 'New tool'.", "Set script, compatibility, surfaces, execution, and save."] },
      {
        task: "Add a script of your own",
        steps: [
          "Copy the file to {base}/scripts/custom/ on the head node (shared across projects, untouched by deploys).",
          "Register it here with the basename only — no path.",
          "It appears in 'Add action' on the rack boards straight away.",
        ],
      },
      {
        task: "Work out which copy of a script will run",
        steps: [
          "Search order is: project-<id>/, then custom/, then <platform-id>/, then multi-platform/ — first match wins.",
          "An env-key override beats all four.",
          "GET /api/catalog/projects/<id>/scripts reports the resolved layout for a project.",
        ],
      },
    ],
    faqs: [
      {
        q: "Do I put the script in the platform folder if the tool supports GB200 and GB300?",
        a: "Only if the script itself differs per platform. The compatibility list decides which clusters are offered the tool; it does not choose the folder. A script that runs on both belongs in the shared defaults (via a merge request) or in custom/ if you wrote it for this deployment.",
      },
      {
        q: "Why can I not edit multi-platform/ or the platform folders?",
        a: "They ship with the app and every project on this server shares them, so an edit changes another customer's run — and the next deploy overwrites it. Put your own script in custom/, change a project's copy in project-<id>/, or open a merge request to change what ships.",
      },
      {
        q: "My tool fails with 'no such file'. Where should the script be?",
        a: "On the head node, under one of the four layers, with exactly the basename you registered. Copying it to the machine running the dashboard is the usual mistake.",
      },
    ],
    tips: [
      "A compiled binary (a nccl-tests build, say) does not live in the script tree: wrap it in a script, or point an env-key override at its absolute path.",
    ],
    related: ["projects", "platformCatalog", "tools"],
    keywords: ["tool", "registry", "script", "sysinfo", "clusterkit", "nvloom", "compatibility", "surface", "execution", "platform", "custom", "basename", "path", "folder", "multi-platform"],
    aria: { label: "Tool registry page", description: "Define tools, their platform compatibility, surfaces, and execution." },
    aiContext:
      "The Tool Registry describes scripts as documents (script basename, compatibility by capability/platform, surfaces, execution mode/target). Tools are filtered to the active platform. The `script` field is a BASENAME resolved through four layers, most specific first: {base}/scripts/project-<project-id>/, then custom/ (operator-added, shared, writable), then <platform-id>/, then multi-platform/ (both shipped and read-only). An env-key override beats all four. Platform compatibility decides which clusters are OFFERED a tool and never which folder the script comes from. Operators add their own scripts to custom/; changing a shipped script for everyone is a merge request.",
  },
  {
    id: "documentation",
    kind: "view",
    title: "Documentation",
    domain: "Platform",
    routePath: "/documentation",
    summary: "The documentation for the version running here, served by this deployment.",
    description:
      "Documentation shows the Sphinx documentation site built from this repository, served by this deployment at /docs/ and framed inside the app. It is served locally rather than linked to the internet because an operator reaches Greenlight through an SSH tunnel on a customer cluster and often cannot reach an external docs site at all — and because the documentation then always matches the code running here.",
    features: [
      "The full documentation site, inside the app rather than a separate browser tab.",
      "Public: no sign-in needed, so it works for an external reader and on an air-gapped host.",
      "Reload, and an explicit 'Open in a new window' for reading it beside the app.",
    ],
    howTo: [
      { task: "Read the documentation", steps: ["Click Documentation in the sidebar.", "Navigate inside the framed site as normal."] },
      { task: "Read it beside the app", steps: ["Open Documentation.", "Click 'Open in a new window'."] },
    ],
    faqs: [
      {
        q: "Why did Documentation used to show the login screen, or 'not authorized'?",
        a: "It was a gated route reached by a plain link, which is two problems. A plain navigation does not go through the app's fetch layer, so it never got the session renewal that keeps the rest of the app signed in, and the link failed about an hour after signing in. Separately, an administrators policy written before the docs service existed refused administrators outright. /docs/ is public now: the documentation is the same text anybody can read in the repository, so gating it protected nothing and broke reading it in two different ways.",
      },
      {
        q: "Is it a problem that the documentation needs no sign-in?",
        a: "It is the point: an external reader or an air-gapped operator can read it, and it cannot log anybody out. The route serves only static files from the Sphinx build, sets no cookie, reads no credential and touches no cluster state, and everything else still requires authentication. backend/tests/test_docs_are_public.py holds each of those.",
      },
      {
        q: "Why is it not a new browser tab any more?",
        a: "The desktop shell has no browser tab to open, so a new tab walked you out of the application entirely. 'Open in a new window' is still there when you want it.",
      },
      {
        q: "The page says the documentation has not been built. What now?",
        a: "static/docs is a build artifact, so a developer tree or a backend-only deployment can genuinely lack it. Build it with sphinx-build -W --keep-going -b html docs static/docs; the container image builds it already.",
      },
    ],
    related: ["apiReference", "help", "sidebar"],
    keywords: ["documentation", "docs", "sphinx", "manual", "guide", "help", "reference", "reading"],
    aria: { label: "Documentation page", description: "The deployment's own documentation site, shown inside the app." },
    aiContext:
      "Documentation frames the Sphinx site served by this deployment at /docs/ inside the app. Served locally so it matches the running code and works on an air-gapped cluster behind an SSH tunnel. /docs/ is PUBLIC (in web_auth.AUTH_BYPASS_ENDPOINTS): the documentation is not secret, and gating it caused two failures — a login screen an hour after signing in, because a plain navigation misses the SPA's refresh-on-401, and 'not authorized' for administrators whose policy predated the docs resource type. It serves only static files, sets no cookie and reads no credential; everything else still requires authentication. 'Open in a new window' remains as a choice.",
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
  {
    id: "apiReference",
    kind: "view",
    title: "API Reference",
    domain: "Platform",
    routePath: "/platform/api",
    summary: "Browsable reference for this deployment's REST API, generated from its own routes.",
    description:
      "API Reference renders the OpenAPI 3.1 document served at /api/openapi.json, which the backend generates from its own Flask route table on each request. Because it is generated rather than maintained, it cannot describe an endpoint this build does not serve, and a new endpoint appears without anyone editing a file. Operations are grouped by endpoint family, searchable by path, verb, group or summary, and each one shows its parameters, request body and the status codes its handler actually returns.",
    features: [
      "Every /api/* operation with its verb, path, summary and full description from the handler's docstring.",
      "Query parameters are the ones each handler actually reads, so the page cannot claim a parameter the code ignores.",
      "Download the raw document as JSON (or /api/openapi.yaml) to generate a client or import into other tooling.",
    ],
    howTo: [
      { task: "Find an endpoint", steps: ["Open API Reference.", "Type a path, verb or keyword in the filter.", "Click an operation to expand its parameters and responses."] },
      { task: "Integrate another system", steps: ["Open API Reference.", "Click 'openapi.json' to download the document.", "Point your client generator at it.", "Authenticate over SRP (POST /api/auth/srp/begin then /finish) and send the access token as 'Authorization: Bearer'."] },
    ],
    related: ["toolRegistry", "settings"],
    keywords: ["api", "openapi", "swagger", "rest", "endpoint", "integration", "client", "spec", "schema", "json", "yaml"],
    aria: { label: "API reference page", description: "Browsable reference for the REST API, generated from the application's own route table." },
    aiContext:
      "API Reference renders /api/openapi.json, an OpenAPI 3.1 document generated from the Flask route table (about 177 paths / 201 operations). Query parameters are derived from what each handler reads; request bodies are free-form JSON by design rather than schema'd. The raw document is available at /api/openapi.json and /api/openapi.yaml, behind the normal auth gate.",
  },
];
