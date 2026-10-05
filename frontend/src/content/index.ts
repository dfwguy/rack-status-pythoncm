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
 * App Knowledge / Context Layer — registry aggregation + helpers.
 *
 * Aggregates every per-domain content array into a single keyed registry and
 * exposes lookup/search helpers used by the AI assistant, the Help page, and
 * the accessibility layer.
 */
import type { HelpContent, HelpRegistry } from "./types";
import { computeContent } from "./registry/compute";
import { rackDetailContent } from "./registry/rackDetail";
import { parserContent } from "./registry/parser";
import { infrastructureContent } from "./registry/infrastructure";
import { observabilityContent } from "./registry/observability";
import { toolsContent } from "./registry/tools";
import { workflowContent } from "./registry/workflow";
import { analyticsContent } from "./registry/analytics";
import { identityContent } from "./registry/identity";
import { platformContent } from "./registry/platform";
import { p2pContent } from "./registry/p2p";
import { shellContent } from "./registry/shell";

export type { HelpContent, HelpRegistry, HelpKind, HelpHowTo, HelpFaq, HelpAria } from "./types";

/** All content entries, in authoring order (used for stable Help-page listing). */
export const ALL_CONTENT: HelpContent[] = [
  ...computeContent,
  ...rackDetailContent,
  ...parserContent,
  ...infrastructureContent,
  ...observabilityContent,
  ...toolsContent,
  ...workflowContent,
  ...analyticsContent,
  ...identityContent,
  ...platformContent,
  ...p2pContent,
  ...shellContent,
];

/** Keyed registry: `id` -> entry. Throws on duplicate ids to keep ids unique. */
export const CONTENT_REGISTRY: HelpRegistry = ALL_CONTENT.reduce<HelpRegistry>((acc, entry) => {
  if (acc[entry.id]) {
    throw new Error(`Duplicate help content id: ${entry.id}`);
  }
  acc[entry.id] = entry;
  return acc;
}, {});

/** Returns a single content entry by id, or undefined. */
export function getContent(id: string): HelpContent | undefined {
  return CONTENT_REGISTRY[id];
}

/**
 * Returns the content entry for a navigable view. View ids match `ViewId`.
 * Rack detail is reached under the `singleRack` view, so callers that have a
 * selected rack should prefer `getContent("rackDetail")`.
 */
export function getViewContent(viewId: string): HelpContent | undefined {
  return CONTENT_REGISTRY[viewId];
}

/** Entries grouped by their `domain` (preserves authoring order within a group). */
export function contentByDomain(): Array<{ domain: string; entries: HelpContent[] }> {
  const order: string[] = [];
  const groups = new Map<string, HelpContent[]>();
  for (const entry of ALL_CONTENT) {
    const domain = entry.domain ?? "Other";
    if (!groups.has(domain)) {
      groups.set(domain, []);
      order.push(domain);
    }
    groups.get(domain)!.push(entry);
  }
  return order.map((domain) => ({ domain, entries: groups.get(domain)! }));
}

/**
 * Case-insensitive search across title, summary, description, features and
 * keywords. Returns entries ranked by a simple field-weighted score.
 */
export function searchContent(query: string): HelpContent[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const terms = q.split(/\s+/).filter(Boolean);

  const scored: Array<{ entry: HelpContent; score: number }> = [];
  for (const entry of ALL_CONTENT) {
    const haystacks: Array<[string, number]> = [
      [entry.title.toLowerCase(), 5],
      [entry.summary.toLowerCase(), 3],
      [entry.keywords.join(" ").toLowerCase(), 3],
      [entry.description.toLowerCase(), 2],
      [entry.features.join(" ").toLowerCase(), 1],
      [entry.howTo.map((h) => `${h.task} ${h.steps.join(" ")}`).join(" ").toLowerCase(), 1],
    ];
    let score = 0;
    for (const term of terms) {
      for (const [text, weight] of haystacks) {
        if (text.includes(term)) score += weight;
      }
    }
    if (score > 0) scored.push({ entry, score });
  }
  scored.sort((a, b) => b.score - a.score);
  return scored.map((s) => s.entry);
}

/**
 * Builds the concise, page-aware context string injected into the AI system
 * prompt via `PageContext.additional_context`. Combines the active view's
 * `aiContext` with the rack-detail context when a rack is selected.
 */
export function buildAIContext(viewId: string, rack?: string): string {
  const parts: string[] = [];

  // Rack detail is reached via the singleRack view; prefer the richer entry.
  const primary =
    viewId === "singleRack" && rack
      ? getContent("rackDetail") ?? getViewContent(viewId)
      : getViewContent(viewId);

  if (primary) {
    parts.push(`Current page: ${primary.title}. ${primary.aiContext}`);
    if (primary.features.length) {
      parts.push(`Key capabilities here: ${primary.features.join("; ")}.`);
    }
  }

  if (rack) {
    parts.push(`The user is focused on rack: ${rack}.`);
  }

  parts.push(
    "When the user asks how to use this page or feature, answer using the capabilities above and give concrete, step-by-step guidance.",
  );

  return parts.join("\n");
}
