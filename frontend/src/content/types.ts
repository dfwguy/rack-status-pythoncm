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
 * App Knowledge / Context Layer — content schema.
 *
 * A single canonical, typed description of every view and component in the app.
 * The same entries feed three consumers:
 *   1. The AI assistant (concise `aiContext` injected as `additional_context`).
 *   2. The in-app Help panel (right-hand KUI SidePanel), which renders the rich fields.
 *   3. Accessibility / screen readers (`aria.label` + `aria.description`).
 *
 * This file (and everything under `content/registry/`) is the source of truth.
 * A generator (`scripts/gen-help-content.mjs`) mirrors it to `help_content.json`
 * at the repo root so the Flask backend can read the identical content.
 */

/** Where this piece of content lives in the app. */
export type HelpKind = "view" | "component" | "dialog" | "shell";

/** A single "how do I…" task with ordered steps. */
export interface HelpHowTo {
  task: string;
  steps: string[];
}

/** A frequently asked question + answer. */
export interface HelpFaq {
  q: string;
  a: string;
}

/** Accessibility metadata used for ARIA labels and screen-reader narration. */
export interface HelpAria {
  /** Concise accessible name (used as `aria-label`). */
  label: string;
  /** Spoken description of the element/page (used via `aria-describedby`). */
  description: string;
}

/**
 * One knowledge entry. `id` is stable and globally unique; for views it matches
 * the `ViewId`, for components it is a camelCase identifier (e.g. `rackTable`).
 */
export interface HelpContent {
  /** Stable unique key. Views use their `ViewId`; components use a camelCase id. */
  id: string;
  kind: HelpKind;
  /** Human-readable title shown in the Help page. */
  title: string;
  /** Domain grouping for the Help page (Compute, Infrastructure, …). */
  domain?: string;
  /** Hash path (without leading `#`) for views, e.g. "/compute/racks". */
  routePath?: string;
  /** One-line summary. */
  summary: string;
  /** A short paragraph describing what this page/component is for. */
  description: string;
  /** Bullet list of notable capabilities/features. */
  features: string[];
  /** Common tasks with ordered steps. */
  howTo: HelpHowTo[];
  /** Optional frequently asked questions. */
  faqs?: HelpFaq[];
  /** Optional usage tips / gotchas. */
  tips?: string[];
  /** Ids of related content entries (cross-links in the Help page). */
  related?: string[];
  /** Search + AI matching keywords. */
  keywords: string[];
  /** Accessibility metadata. */
  aria: HelpAria;
  /** Concise text optimized for the LLM system prompt. */
  aiContext: string;
}

/** A keyed map of every content entry. */
export type HelpRegistry = Record<string, HelpContent>;
