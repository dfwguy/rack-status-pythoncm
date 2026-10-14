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
 * Presentation rules for the deployment tracker: colours, labels, filtering, percentages.
 *
 * What a state *means* is not here -- it arrives in `meta.definitions`, written by the backend
 * classifier, so the legend cannot drift from the code that decides it.
 */
import type {
  DeploymentState,
  DeploymentTrackerReport,
  StateCounts,
  TrackerNode,
  TrackerRack,
  TrackerRow,
  TrackerUnit,
} from "../types";

export const STATE_ORDER: DeploymentState[] = [
  "completed",
  "in_progress",
  "remediation",
  "not_started",
  "handed_over",
];

export const STATE_LABEL: Record<DeploymentState, string> = {
  completed: "Completed",
  in_progress: "In progress",
  remediation: "In remediation",
  not_started: "Not started",
  handed_over: "Handed over",
};

/** Short column headings for the dense per-row cards. */
export const STATE_ABBR: Record<DeploymentState, string> = {
  completed: "Done",
  in_progress: "Prog",
  remediation: "Rem",
  not_started: "N.S.",
  handed_over: "H.O.",
};

/**
 * Fill colours for tiles and bars: Kaizen palette tokens, no hex fallback. "Not started" is the
 * empty track itself, so a bar fills as work happens rather than swapping one grey for another.
 * Yellow is the 200 step because the 300-500 steps are orange-to-vermilion and sit beside red.
 */
export const STATE_COLOR: Record<DeploymentState, string> = {
  completed: "var(--color-green-300)",
  in_progress: "var(--color-yellow-200)",
  remediation: "var(--color-red-500)",
  not_started: "var(--background-color-component-track)",
  handed_over: "var(--color-teal-400)",
};

/**
 * Text colours for numbers printed in a state's colour. Separate from the fills because a fill
 * only needs to be told apart, while text must stay legible on a light page too -- so these
 * are the theme-aware feedback tokens.
 */
export const STATE_TEXT: Record<DeploymentState, string> = {
  completed: "var(--text-color-feedback-success)",
  in_progress: "var(--text-color-feedback-warning)",
  remediation: "var(--text-color-feedback-danger)",
  not_started: "var(--text-color-secondary)",
  handed_over: "var(--color-teal-500)",
};

/** KUI `Tag` colours for a group's status. */
export const STATE_TAG: Record<DeploymentState, "green" | "yellow" | "red" | "gray" | "teal"> = {
  completed: "green",
  in_progress: "yellow",
  remediation: "red",
  not_started: "gray",
  handed_over: "teal",
};

/**
 * A percentage as the page prints it: whole numbers, rounded **down**, so a unit with one node
 * left never reads "100%". Values between 0 and 1 keep one decimal so early progress is visible.
 */
export function formatPercent(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0%";
  if (value < 1) return `${value.toFixed(1)}%`;
  return `${Math.floor(value)}%`;
}

export function formatCount(n: number): string {
  return n.toLocaleString("en-US");
}

/** "1 rack", "7 racks". */
export function plural(n: number, noun: string): string {
  return `${formatCount(n)} ${noun}${n === 1 ? "" : "s"}`;
}

/** The non-zero segments of a stacked bar, in display order. */
export function segments(counts: StateCounts): { state: DeploymentState; count: number; share: number }[] {
  const total = STATE_ORDER.reduce((sum, s) => sum + (counts[s] || 0), 0);
  if (!total) return [];
  return STATE_ORDER.filter((s) => counts[s] > 0).map((state) => ({
    state,
    count: counts[state],
    share: (100 * counts[state]) / total,
  }));
}

export interface TrackerFilter {
  state: DeploymentState | "all";
  unit: string | "all";
  query: string;
}

export const NO_FILTER: TrackerFilter = { state: "all", unit: "all", query: "" };

/** Lower-cased words a search box can hit for one node. */
function haystack(unit: TrackerUnit, row: TrackerRow, rack: TrackerRack, node: TrackerNode): string {
  return [unit.label, row.label, rack.label, node.node, node.reason, node.slurm].join(" ").toLowerCase();
}

/** Whether one node passes the state and search filters (the unit filter is applied per unit). */
export function nodeMatches(
  filter: TrackerFilter,
  unit: TrackerUnit,
  row: TrackerRow,
  rack: TrackerRack,
  node: TrackerNode,
): boolean {
  if (filter.state !== "all" && node.state !== filter.state) return false;
  const q = filter.query.trim().toLowerCase();
  if (!q) return true;
  return q.split(/\s+/).every((term) => haystack(unit, row, rack, node).includes(term));
}

export function unitVisible(filter: TrackerFilter, unit: TrackerUnit): boolean {
  return filter.unit === "all" || filter.unit === unit.id;
}

/** How many nodes the current filter shows, for the "N nodes shown" caption. */
export function countShown(units: TrackerUnit[], filter: TrackerFilter): number {
  let n = 0;
  for (const unit of units) {
    if (!unitVisible(filter, unit)) continue;
    for (const row of unit.rows)
      for (const rack of row.racks)
        for (const node of rack.nodes) if (nodeMatches(filter, unit, row, rack, node)) n += 1;
  }
  return n;
}

/** A timestamp as a reader on any continent can compare: UTC, minute precision. */
export function formatUtc(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

/** The one-line description both shells put under their title. */
export function trackerSubtitle(report: DeploymentTrackerReport): string {
  const s = report.summary;
  return `Compute node bring-up · ${formatCount(s.total)} nodes · ${s.units} scalable unit${s.units === 1 ? "" : "s"} · ${formatCount(s.racks)} racks · data as of ${formatUtc(report.meta.generated_at)}`;
}
