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
 * Knowledge entries for the app shell and cross-cutting chrome: layout, sidebar,
 * toolbar, breadcrumbs, page header, banners, the AI assistant widget, and
 * shared status components.
 */
import type { HelpContent } from "../types";

export const shellContent: HelpContent[] = [
  {
    id: "appLayout",
    kind: "shell",
    title: "App layout",
    domain: "Shell",
    summary: "The overall page shell: sidebar, toolbar, breadcrumbs, content, footer.",
    description:
      "AppLayout is the flex shell that holds the permanent sidebar, the top toolbar, optional breadcrumbs, the scrollable main content area, and a footer showing the backend mode.",
    features: ["Permanent sidebar + top toolbar.", "Breadcrumbs and scrollable content.", "Footer with backend mode."],
    howTo: [{ task: "Understand the layout", steps: ["Sidebar = navigation.", "Toolbar = page title + global actions.", "Main = the current page."] }],
    related: ["sidebar", "toolbar", "appBreadcrumbs"],
    keywords: ["layout", "shell", "chrome", "structure", "footer"],
    aria: { label: "Application layout", description: "Page shell with sidebar navigation, top toolbar, breadcrumbs, content area, and footer." },
    aiContext: "AppLayout is the shell: permanent sidebar, top toolbar, breadcrumbs, scrollable main content, and a footer showing backend mode.",
  },
  {
    id: "sidebar",
    kind: "shell",
    title: "Sidebar navigation",
    domain: "Shell",
    summary: "Left navigation grouped by domain, with Settings, Docs, and Help.",
    description:
      "The Sidebar is the left navigation. It groups Nscale destinations by domain (Compute, Infrastructure, Observability, Tools, Analytics & Benchmarks, Platform) and has bottom items for Settings, Documentation, and Help. It can collapse to an icon rail.",
    features: ["Domain-grouped navigation.", "Collapsible to an icon rail.", "Settings, Documentation, and Help links."],
    howTo: [
      { task: "Navigate to a page", steps: ["Open the relevant domain group in the sidebar.", "Click the destination."] },
      { task: "Collapse the sidebar", steps: ["Use the collapse toggle to switch to the icon rail."] },
    ],
    related: ["appLayout", "help", "settings"],
    keywords: ["sidebar", "navigation", "menu", "nav", "drawer", "collapse"],
    aria: { label: "Sidebar navigation", description: "Left navigation grouped by domain with Settings, Documentation, and Help." },
    aiContext:
      "The Sidebar is the Nscale navigation grouped by domain, including Tools and Platform; it is collapsible to an icon rail.",
  },
  {
    id: "toolbar",
    kind: "shell",
    title: "Toolbar",
    domain: "Shell",
    summary: "Top bar: page title, refresh, theme, scheduler dialogs, terminal, and logout.",
    description:
      "The Toolbar is the top app bar. It shows the current page title and global actions: refresh, theme toggle, active jobs, node availability, head-node terminal, log export, active users, and logout.",
    features: ["Page title.", "Refresh and theme toggle.", "Active-jobs/node-availability dialogs, terminal, log export, active users, logout."],
    howTo: [
      { task: "Refresh data", steps: ["Click the refresh action in the toolbar."] },
      { task: "Toggle dark/light theme", steps: ["Click the theme toggle in the toolbar."] },
    ],
    related: ["appLayout", "squeueDialog", "sinfoDialog"],
    keywords: ["toolbar", "appbar", "header", "refresh", "theme", "logout", "actions"],
    aria: { label: "Top toolbar", description: "Top bar with the page title and global actions: refresh, theme, scheduler dialogs, terminal, log export, and logout." },
    aiContext:
      "The Toolbar is the top app bar with the page title and global actions; periodic state is PythonCM-backed, while its interactive terminal is an explicit SSH fallback.",
  },
  {
    id: "appBreadcrumbs",
    kind: "shell",
    title: "Breadcrumbs",
    domain: "Shell",
    summary: "Navigation trail showing Domain / View / rack.",
    description: "AppBreadcrumbs derives a navigation trail from the URL (Domain / View, plus the rack on rack-detail), hidden on Settings.",
    features: ["URL-derived trail.", "Includes rack on rack-detail."],
    howTo: [{ task: "Go up a level", steps: ["Click a parent crumb in the trail."] }],
    related: ["appLayout", "sidebar"],
    keywords: ["breadcrumbs", "trail", "navigation", "domain", "view"],
    aria: { label: "Breadcrumbs", description: "Navigation trail showing the current domain, view, and selected rack." },
    aiContext: "AppBreadcrumbs is the URL-derived navigation trail (Domain / View, plus rack on rack-detail).",
  },
  {
    id: "pageHeader",
    kind: "shell",
    title: "Page header",
    domain: "Shell",
    summary: "Reusable page title + description, help, and action slot.",
    description:
      "PageHeader is the reusable header at the top of each page: a title and optional description, an optional contextual help button and 'Ask AI about this page' action, plus a slot for page-level actions. It also wires accessibility descriptions for screen readers.",
    features: ["Title + description.", "Contextual help button.", "Ask-AI-about-this-page action.", "Action slot.", "Accessibility descriptions."],
    howTo: [
      { task: "Get help for a page", steps: ["Click the help icon in the page header.", "The Help page opens to that page's topic."] },
      { task: "Ask the AI about a page", steps: ["Click the Ask AI action in the header.", "The assistant opens seeded with a question about the page."] },
    ],
    related: ["help", "aiChatWidget"],
    keywords: ["page header", "title", "description", "help", "ask ai", "actions"],
    aria: { label: "Page header", description: "Page title and description with contextual help and an ask-AI action." },
    aiContext:
      "PageHeader is the reusable page title/description header with a contextual help button, an 'Ask AI about this page' action, and an actions slot; it also provides accessibility descriptions.",
  },
  {
    id: "previewBanner",
    kind: "component",
    title: "Preview banner",
    domain: "Shell",
    summary: "Banner shown on preview/mock pages.",
    description: "PreviewBanner is shown on preview pages to indicate that data is mock or not yet live, including the Terminal placeholder when applicable.",
    features: ["Indicates preview/mock pages."],
    howTo: [{ task: "Know if a page is live", steps: ["Look for the preview banner.", "If present, the page uses mock data."] }],
    related: ["infraEthernet", "obsMetrics", "terminal"],
    keywords: ["preview", "banner", "mock", "placeholder"],
    aria: { label: "Preview banner", description: "Banner indicating a page is a preview backed by mock data." },
    aiContext: "PreviewBanner marks preview pages as mock/not-yet-live.",
  },
  {
    id: "aiChatWidget",
    kind: "component",
    title: "AI assistant",
    domain: "Shell",
    summary: "Floating AI chat that is aware of the current page.",
    description:
      "The AI assistant is a floating chat (a green button bottom-right) available on every page. It knows which page you are on and answers questions about rack validation, PythonCM/direct execution, BCM, NEMO, and how to use the app's features. Open it, type a question, and it streams an answer; you can minimize, expand full-screen, or clear the conversation.",
    features: [
      "Page-aware: knows the current page/rack for better answers.",
      "Streams answers; can stop, clear, minimize, or expand.",
      "Configured in Settings (endpoint, model, key, custom rules).",
    ],
    howTo: [
      { task: "Ask the assistant a question", steps: ["Click the floating AI button (bottom-right).", "Type your question and send.", "Read the streamed answer."] },
      { task: "Get help about the current page", steps: ["Open the assistant while on the page.", "Ask 'how do I use this page?' — it answers using the page's context."] },
    ],
    tips: ["The assistant automatically receives the current page's context, so you can ask page-specific questions without explaining where you are."],
    related: ["help", "settings", "aiConfigurationPanel", "pageHeader"],
    keywords: ["ai", "assistant", "chat", "bot", "help", "questions", "copilot"],
    aria: { label: "AI assistant", description: "Floating, page-aware AI chat that answers questions and explains how to use the app." },
    aiContext:
      "The AI assistant is a floating, page-aware chat available on every page. It answers questions about rack validation, PythonCM/direct execution, BCM, NEMO, and how to use app features, using the current page's context. Configured in Settings.",
  },
  {
    id: "errorBanner",
    kind: "component",
    title: "Error banner",
    domain: "Shell",
    summary: "Banner shown when an API load fails.",
    description: "ErrorBanner displays a clear error message when loading data from an API fails, often with a hint to retry.",
    features: ["Shows API load errors.", "Prompts to retry."],
    howTo: [{ task: "Recover from a load error", steps: ["Read the error banner.", "Use the toolbar refresh to retry."] }],
    related: ["toolbar", "loadingSkeleton"],
    keywords: ["error", "banner", "failure", "api", "retry"],
    aria: { label: "Error banner", description: "Error message shown when loading data fails." },
    aiContext: "ErrorBanner shows a clear error message when an API load fails, usually with a retry hint.",
  },
  {
    id: "footerNotes",
    kind: "component",
    title: "Footer notes",
    domain: "Shell",
    summary: "Server-provided footnotes under the racks table.",
    description: "FooterNotes renders server-provided footnotes beneath the racks table (e.g. caveats about the data or legend explanations).",
    features: ["Server-provided footnotes.", "Context/legend for the table."],
    howTo: [{ task: "Read table caveats", steps: ["Scroll below the racks table.", "Read the footer notes."] }],
    related: ["singleRack", "rackTable"],
    keywords: ["footer", "notes", "footnotes", "legend", "caveats"],
    aria: { label: "Footer notes", description: "Server-provided footnotes shown beneath the racks table." },
    aiContext: "FooterNotes shows server-provided footnotes beneath the racks table (caveats/legend).",
  },
  {
    id: "loadingSkeleton",
    kind: "component",
    title: "Loading skeleton",
    domain: "Shell",
    summary: "Full-page placeholder while data loads.",
    description: "LoadingSkeleton is the full-page placeholder shown while rack status data is loading.",
    features: ["Indicates loading state.", "Mimics the page layout."],
    howTo: [{ task: "Know data is loading", steps: ["See the skeleton placeholder.", "Wait for it to resolve into content."] }],
    related: ["singleRack", "errorBanner"],
    keywords: ["loading", "skeleton", "placeholder", "spinner"],
    aria: { label: "Loading placeholder", description: "Full-page placeholder shown while data is loading." },
    aiContext: "LoadingSkeleton is the full-page loading placeholder shown while rack status data loads.",
  },
];
