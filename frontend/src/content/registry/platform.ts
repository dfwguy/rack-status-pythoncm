/**
 * Knowledge entries for the Platform domain: Settings, the AI configuration
 * panel, and the Help page itself.
 */
import type { HelpContent } from "../types";

export const platformContent: HelpContent[] = [
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
