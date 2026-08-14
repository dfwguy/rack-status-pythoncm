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
 * Colour scales and view definitions for the cluster node heatmap.
 *
 * One square per node. The selected view drives the fill; node up/down state is always drawn
 * as the border, so a single picture answers "is it alive" and "how fast is it" together.
 *
 * Metric views are scaled against the fleet's own observed range rather than a fixed ceiling.
 * That is deliberate: NCCL busbw is reported in GB/s (a healthy GB300 rack is ~690 GB/s, i.e.
 * ~5.5 Tbps), so a "0 to 1 Tbps" axis would saturate every cell at ~125 GB/s. Auto-scaling
 * keeps the gradient meaningful on any platform and any unit, and the legend states the range
 * it used so the colours are never ambiguous.
 */
import { relativeLuminanceRgb } from "./ylGnHeatmap";
import type { BringupFleet, BringupFleetNode } from "../types/bringup";
import type { NodeInfo } from "../components/rack/NodeSelector";

/** White → hot blue, for "more is better" throughput metrics. */
const WHITE_BLUE_STOPS: Array<{ t: number; r: number; g: number; b: number }> = [
  { t: 0, r: 248, g: 250, b: 252 },
  { t: 0.25, r: 186, g: 214, b: 240 },
  { t: 0.5, r: 96, g: 165, b: 250 },
  { t: 0.75, r: 37, g: 99, b: 235 },
  { t: 1, r: 12, g: 42, b: 138 },
];

function interpolate(
  stops: Array<{ t: number; r: number; g: number; b: number }>,
  norm01: number,
): [number, number, number] {
  const t = Math.max(0, Math.min(1, Number.isFinite(norm01) ? norm01 : 0));
  let lo = stops[0];
  let hi = stops[stops.length - 1];
  for (let s = 0; s < stops.length - 1; s++) {
    if (t >= stops[s].t && t <= stops[s + 1].t) {
      lo = stops[s];
      hi = stops[s + 1];
      break;
    }
  }
  const span = hi.t - lo.t || 1;
  const u = (t - lo.t) / span;
  return [
    Math.round(lo.r + (hi.r - lo.r) * u),
    Math.round(lo.g + (hi.g - lo.g) * u),
    Math.round(lo.b + (hi.b - lo.b) * u),
  ];
}

export function throughputColor(norm01: number): string {
  const [r, g, b] = interpolate(WHITE_BLUE_STOPS, norm01);
  return `rgb(${r},${g},${b})`;
}

/**
 * The scale as a CSS gradient, for a gauge fill.
 *
 * Painted across the whole cell and then masked to the fill length, rather than colouring the bar
 * a single value-dependent shade. With a solid fill, values clustered near the ceiling produce
 * near-identical bars — 690 and 630 GB/s are both "long and blue" — whereas revealing more or
 * less of a fixed gradient makes the bar's right edge differ row to row.
 */
export const GAUGE_GRADIENT = `linear-gradient(to right, ${WHITE_BLUE_STOPS.map(
  (s) => `rgb(${s.r},${s.g},${s.b}) ${Math.round(s.t * 100)}%`,
).join(", ")})`;

export function textColorFor(norm01: number): string {
  const [r, g, b] = interpolate(WHITE_BLUE_STOPS, norm01);
  return relativeLuminanceRgb(r, g, b) > 0.52 ? "#111111" : "#f8fafc";
}

/** Fill for the up/down view. */
export const STATUS_FILL: Record<NodeInfo["status"], string> = {
  up: "#16a34a",
  down: "#dc2626",
  inst: "#f59e0b",
  unknown: "#6b7280",
};

/** Border colour, drawn on every view so liveness is never hidden by the metric. */
export const STATUS_BORDER: Record<NodeInfo["status"], string> = {
  up: "#15803d",
  down: "#b91c1c",
  inst: "#b45309",
  unknown: "#4b5563",
};

/**
 * Border colour per benchmark verdict, used on a metric view.
 *
 * On a metric view the border answers "did this benchmark pass on this node", which is the
 * question being asked; node liveness is one click away on the status view. Green/red match the
 * up/down borders so the palette stays consistent across views.
 */
export const VERDICT_BORDER: Record<string, string> = {
  PASS: "#15803d",
  DONE: "#15803d",
  FAIL: "#b91c1c",
  RETRY: "#b45309",
  INCOMPLETE: "#b45309",
  RUNNING: "#1d4ed8",
  NOT_RUN: "#4b5563",
};
/** Verdict we do not recognise, or a node the fleet feed never mentioned. */
export const VERDICT_BORDER_UNKNOWN = "#4b5563";

/**
 * Border for a cell: node up/down on the status view, benchmark verdict on a metric view.
 *
 * Deliberately one function so the two views cannot drift apart, and so the legend and the cells
 * are guaranteed to describe the same thing.
 */
export function borderFor(
  view: HeatmapView,
  node: BringupFleetNode | undefined,
  status: NodeInfo["status"],
): string {
  if (!view.column) return STATUS_BORDER[status];
  const verdict = node ? verdictOf(node, view.column) : "";
  return VERDICT_BORDER[verdict] ?? VERDICT_BORDER_UNKNOWN;
}

export const STATUS_VIEW_ID = "status";
export const COMPLETION_VIEW_ID = "completion";

/** How far through its test plan a node is, ignoring whether the tests passed. */
export type NodeCompletion = "all_done" | "partial" | "none_run" | "no_data";

/**
 * Fill for the completion view: green all finished, red nothing started, yellow in between.
 *
 * Deliberately a different question from every other view — those ask "is it healthy" or "how
 * fast is it", this one asks "has it been tested yet", which is what a bring-up is tracking
 * day to day. A node can be solid green here and still have failed every test; the border
 * (up/down) and the per-benchmark views answer that.
 */
export const COMPLETION_FILL: Record<NodeCompletion, string> = {
  all_done: "#16a34a",
  partial: "#eab308",
  none_run: "#dc2626",
  no_data: "#6b7280",
};

/** A test that reached a verdict, pass or fail — it is done either way. */
const FINISHED_VERDICTS = new Set(["PASS", "DONE", "FAIL"]);
/** Started but no answer yet, so it counts as neither done nor untouched. */
const PENDING_VERDICTS = new Set(["RUNNING", "RETRY", "INCOMPLETE"]);

export function completionOf(node: BringupFleetNode | undefined): NodeCompletion {
  const jobs = node?.jobs ?? [];
  if (!jobs.length) return "no_data";
  let finished = 0;
  let pending = 0;
  for (const job of jobs) {
    const verdict = (job.status || "").toUpperCase();
    if (FINISHED_VERDICTS.has(verdict)) finished += 1;
    else if (PENDING_VERDICTS.has(verdict)) pending += 1;
  }
  if (finished === jobs.length) return "all_done";
  if (finished === 0 && pending === 0) return "none_run";
  return "partial";
}

/** What kind of question a view answers, which decides how its cells are painted. */
export type HeatmapViewKind = "status" | "completion" | "metric";

export interface HeatmapView {
  id: string;
  label: string;
  /** Metric column this view reads, or null for the status and completion views. */
  column: string | null;
  unit: string;
  kind: HeatmapViewKind;
}

/**
 * Available views: node status, test completion, then one per column that produces a number.
 *
 * Built from the fleet payload rather than hardcoded, so a site running different benchmarks
 * gets its own views without a code change.
 */
export function heatmapViews(fleet: BringupFleet | null): HeatmapView[] {
  const views: HeatmapView[] = [
    { id: STATUS_VIEW_ID, label: "Node status (up / down)", column: null, unit: "", kind: "status" },
    {
      id: COMPLETION_VIEW_ID,
      label: "Test completion (done / partial / not run)",
      column: null,
      unit: "",
      kind: "completion",
    },
  ];
  for (const col of fleet?.columns ?? []) {
    const unit = fleet?.metric_labels?.[col];
    if (unit) views.push({ id: col, label: `${col} — ${unit}`, column: col, unit, kind: "metric" });
  }
  return views;
}

export interface MetricRange {
  min: number;
  max: number;
}

/**
 * Gauge ceiling when the fleet has measured nothing yet, in the metric's own unit.
 *
 * Only reachable before any result exists — with even one measurement the observed max is used —
 * so it exists to keep a gauge from dividing by zero rather than to assert a target. Note the
 * unit: NCCL busbw is GB/s, and a healthy GB300 node is ~690 GB/s (~5.5 Tbps), so this is a
 * generous headroom figure rather than a line anyone should pass or fail against.
 */
export const FALLBACK_METRIC_MAX = 1800;

/** Gauge ceiling for a column: the fleet's observed max, else the fallback. */
export function scaleTop(range: MetricRange | null): number {
  return range && range.max > 0 ? range.max : FALLBACK_METRIC_MAX;
}

/** Fraction of the gauge to fill for a value, 0–1. */
export function gaugeFraction(value: number, range: MetricRange | null): number {
  const top = scaleTop(range);
  return Math.max(0, Math.min(1, value / top));
}

/** Observed range for a column across the fleet; null when nothing measured it. */
export function metricRange(fleet: BringupFleet | null, column: string | null): MetricRange | null {
  if (!fleet || !column) return null;
  const values: number[] = [];
  for (const node of fleet.nodes) {
    const v = metricOf(node, column);
    if (v !== null) values.push(v);
  }
  if (!values.length) return null;
  return { min: Math.min(...values), max: Math.max(...values) };
}

export function metricOf(node: BringupFleetNode, column: string | null): number | null {
  if (!column) return null;
  const job = node.jobs.find((j) => j.name === column);
  if (!job || job.metric === null) return null;
  const n = Number(job.metric);
  return Number.isFinite(n) ? n : null;
}

export function verdictOf(node: BringupFleetNode, column: string | null): string {
  if (!column) return "";
  return (node.jobs.find((j) => j.name === column)?.status || "").toUpperCase();
}

/** Cell background when there is a value to show; the bar carries the magnitude. */
export const CELL_TRACK = "#1f2530";

/** Unfilled part of a gauge. Darker than the cell so the fill boundary is visible. */
export const GAUGE_TRACK = "#0b0f14";

export interface CellPaint {
  /** Bar length as a fraction of the cell, 0–1. */
  bar: number;
  /** Bar colour. */
  barColor: string;
  /** Cell background — tinted when the cell has a verdict but no number. */
  cellBg: string;
  /** Normalised position in the range, or null when nothing was measured. */
  norm: number | null;
}

/**
 * How to draw one cell: a load bar inside a bordered box.
 *
 * Two channels with their own geometry so they never compete for the same pixels — the border
 * (see :func:`borderFor`) carries the verdict, and the bar length plus colour is the magnitude.
 *
 * A cell with no number draws an empty bar. It used to tint the background per verdict too, but
 * once the border took that job the tint was saying the same thing twice, and an unrecognised
 * verdict fell through to the same near-white as "never ran" — so pink-vs-white was ambiguous
 * exactly where it mattered. The border now explains why a bar is empty.
 */
export function paintCell(
  view: HeatmapView,
  node: BringupFleetNode | undefined,
  status: NodeInfo["status"],
  range: MetricRange | null,
): CellPaint {
  if (view.kind === "completion") {
    return { bar: 1, barColor: COMPLETION_FILL[completionOf(node)], cellBg: CELL_TRACK, norm: null };
  }
  if (!view.column) {
    // Status view: the bar restates the border so the view still reads at a glance.
    return { bar: 1, barColor: STATUS_FILL[status], cellBg: CELL_TRACK, norm: null };
  }
  const value = node ? metricOf(node, view.column) : null;
  if (value === null) {
    return { bar: 0, barColor: "transparent", cellBg: CELL_TRACK, norm: null };
  }
  // Anchor at zero so gauges are comparable in absolute terms; only the top end adapts.
  const norm = gaugeFraction(value, range);
  return { bar: norm, barColor: throughputColor(norm), cellBg: CELL_TRACK, norm };
}

/** Plain-language verdict for a table cell, and the tone to show it in. */
export interface VerdictLabel {
  label: string;
  tone: "green" | "red" | "yellow" | "blue" | "gray";
}

/**
 * Map a bring-up verdict onto the words an operator uses.
 *
 * "Untested" rather than the raw NOT_RUN, and RETRY/INCOMPLETE collapse to "Running" because
 * from outside they mean the same thing: the answer is not in yet, so the cell must not read as
 * either a pass or a failure.
 */
export function verdictLabel(verdict: string): VerdictLabel {
  switch ((verdict || "").toUpperCase()) {
    case "DONE":
    case "PASS":
      return { label: "Pass", tone: "green" };
    case "FAIL":
      return { label: "Failed", tone: "red" };
    case "RUNNING":
      return { label: "Running", tone: "blue" };
    case "RETRY":
    case "INCOMPLETE":
      return { label: "Running", tone: "yellow" };
    case "NOT_RUN":
    case "":
      return { label: "Untested", tone: "gray" };
    default:
      return { label: verdict, tone: "gray" };
  }
}

/**
 * Benchmarks that get a fixed leading position in the table.
 *
 * The bring-up CSV's own order puts dcgm first and buries the throughput numbers; these are the
 * ones an operator reads first, so they are pinned. Anything not listed keeps its original
 * relative order after them, so a site adding a benchmark still sees it without a code change.
 */
export const PREFERRED_METRIC_ORDER = [
  "allreduce",
  "alltoall",
  "nvbandwidth",
  "allreduce-forcesharp",
];

/** Order columns with the preferred benchmarks first, then everything else as given. */
export function orderColumns(columns: string[]): string[] {
  const pinned = PREFERRED_METRIC_ORDER.filter((c) => columns.includes(c));
  return [...pinned, ...columns.filter((c) => !pinned.includes(c))];
}

/** Columns that carry a number, and those that are status-only (dcgm and friends). */
export function splitColumns(fleet: BringupFleet | null): {
  metric: string[];
  statusOnly: string[];
} {
  const metric: string[] = [];
  const statusOnly: string[] = [];
  for (const col of orderColumns(fleet?.columns ?? [])) {
    (fleet?.metric_labels?.[col] ? metric : statusOnly).push(col);
  }
  return { metric, statusOnly };
}

/**
 * Sort rank for a verdict: problems first when ascending.
 *
 * Sorting a status column is nearly always a hunt for what is broken, so ascending puts failures
 * at the top rather than sorting the words alphabetically.
 */
export function verdictRank(verdict: string): number {
  switch (verdictLabel(verdict).label) {
    case "Failed":
      return 0;
    case "Running":
      return 1;
    case "Untested":
      return 2;
    case "Pass":
      return 3;
    default:
      return 4;
  }
}

/** Sort rank for node status: unhealthy first when ascending. */
export function statusRank(status: NodeInfo["status"]): number {
  return { down: 0, inst: 1, unknown: 2, up: 3 }[status] ?? 4;
}

/**
 * Comparator for a metric column: numeric, with unmeasured nodes always last.
 *
 * Pinning "no value" to the bottom in both directions is what makes "sort to find the slowest"
 * useful — otherwise descending shows the fastest and ascending shows a screen of blanks.
 */
export function compareMetric(
  a: number | null,
  b: number | null,
  direction: "asc" | "desc",
): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  return direction === "asc" ? a - b : b - a;
}

/** Every measured job on a node, for the tooltip. */
export function nodeMetrics(
  node: BringupFleetNode | undefined,
): Array<{ name: string; status: string; metric: string | null; unit: string; reason: string }> {
  return (node?.jobs ?? []).map((j) => ({
    name: j.name,
    status: (j.status || "").toUpperCase(),
    metric: j.metric,
    unit: j.metric_label ?? "",
    reason: j.reason || "",
  }));
}

/**
 * Solid colour for a cell, used where there is no room for a gauge.
 *
 * The compact layout fits the whole floor on one screen by shrinking cells to a few pixels, at
 * which point a bar inside a track is unreadable. Metric views keep their magnitude by taking
 * the shade the gauge would have ended on, so the same scale and legend still apply.
 */
export function cellFill(
  view: HeatmapView,
  node: BringupFleetNode | undefined,
  status: NodeInfo["status"],
  range: MetricRange | null,
): string {
  if (view.kind === "completion") return COMPLETION_FILL[completionOf(node)];
  if (view.kind === "status") return STATUS_FILL[status];
  const value = node ? metricOf(node, view.column) : null;
  if (value === null) return GAUGE_TRACK;
  return throughputColor(gaugeFraction(value, range));
}

/**
 * Whether a cell reads as good, bad, or somewhere in between on the current view.
 *
 * One function so a rack's summary counts can never disagree with the colours in that rack —
 * the counts are literally a tally of what the cells are showing.
 */
export type CellTone = "good" | "warn" | "bad" | "none";

export function cellTone(
  view: HeatmapView,
  node: BringupFleetNode | undefined,
  status: NodeInfo["status"],
): CellTone {
  if (view.kind === "completion") {
    const done = completionOf(node);
    if (done === "all_done") return "good";
    if (done === "partial") return "warn";
    if (done === "none_run") return "bad";
    return "none";
  }
  if (view.kind === "status") {
    if (status === "up") return "good";
    if (status === "down") return "bad";
    if (status === "inst") return "warn";
    return "none";
  }
  switch (verdictLabel(node ? verdictOf(node, view.column) : "").label) {
    case "Pass":
      return "good";
    case "Failed":
      return "bad";
    case "Running":
      return "warn";
    default:
      return "none";
  }
}

export interface ToneCounts {
  good: number;
  warn: number;
  bad: number;
  none: number;
}

export function toneCounts(tones: CellTone[]): ToneCounts {
  const counts: ToneCounts = { good: 0, warn: 0, bad: 0, none: 0 };
  for (const tone of tones) counts[tone] += 1;
  return counts;
}

export const TONE_FILL: Record<CellTone, string> = {
  good: "#16a34a",
  warn: "#eab308",
  bad: "#dc2626",
  none: "#6b7280",
};

/**
 * The row a rack sits in, i.e. its id with the trailing rack number removed
 * ("dh01-h01" -> "dh01-h"). Returns "" when the id has no trailing number, in which case the
 * caller should skip row grouping rather than invent one.
 */
export function rackRowOf(rack: string): string {
  const m = /^(.*?[^0-9])[0-9]+$/.exec(rack.trim());
  return m ? m[1] : "";
}

/**
 * Group racks into floor rows, preserving order. Racks whose id has no trailing number fall
 * into a single unnamed group so nothing is dropped from the picture.
 */
export function groupRacksByRow<T extends { rack: string }>(
  racks: T[],
): Array<{ row: string; racks: T[] }> {
  const order: string[] = [];
  const byRow = new Map<string, T[]>();
  for (const entry of racks) {
    const row = rackRowOf(entry.rack);
    if (!byRow.has(row)) {
      byRow.set(row, []);
      order.push(row);
    }
    byRow.get(row)!.push(entry);
  }
  return order.map((row) => ({ row, racks: byRow.get(row)! }));
}

/**
 * The part of a rack id worth printing on a tile ("ink50-dh01-h01" -> "h01").
 *
 * The row heading above the tiles already carries the prefix, so repeating it on all 49 blocks
 * is noise that also forces the label — and therefore the whole tile — to stay small. The full
 * id stays available on hover.
 */
export function shortRackLabel(rack: string): string {
  const tail = rack.split("-").pop() ?? "";
  return tail || rack;
}

/**
 * The part of a node id that identifies it within its rack ("dh01-h01-14" -> "14").
 *
 * Compact cells are too small for a full hostname but wide enough for a slot number, which is
 * what an operator reads off the rack door. Returns "" when the remainder is too long to fit,
 * so an unfamiliar naming scheme degrades to an unlabelled square instead of a smear of text.
 */
export function shortNodeLabel(nodeId: string, rack: string): string {
  let rest = nodeId.startsWith(rack) ? nodeId.slice(rack.length) : nodeId;
  rest = rest.replace(/^[^A-Za-z0-9]+/, "");
  const tail = rest.split("-").pop() ?? "";
  return tail.length > 0 && tail.length <= 3 ? tail : "";
}

/**
 * Compact layout shape: racks tiled 8 across, each rack's trays 3 across.
 *
 * 8 x 3 fits a 49-rack floor on one screen without horizontal scrolling at 1280px, and 3 keeps
 * an 18-node rack six rows tall — roughly the proportions of the rack itself, so the picture
 * matches what someone standing in the aisle sees.
 */
export const COMPACT_RACKS_PER_ROW = 8;
export const COMPACT_NODES_PER_ROW = 3;

/** Split a rack's nodes into fixed-width rows, the way trays stack in the rack. */
export function chunkNodes<T>(nodes: T[], perRow: number): T[][] {
  const width = Math.max(1, perRow);
  const out: T[][] = [];
  for (let i = 0; i < nodes.length; i += width) out.push(nodes.slice(i, i + width));
  return out;
}

/** Group nodes by rack for display, preserving the order racks appear in. */
export function groupByRack(
  nodeIds: string[],
  rackOf: (nodeId: string) => string,
): Array<{ rack: string; nodes: string[] }> {
  const order: string[] = [];
  const byRack = new Map<string, string[]>();
  for (const id of nodeIds) {
    const rack = rackOf(id) || "unassigned";
    if (!byRack.has(rack)) {
      byRack.set(rack, []);
      order.push(rack);
    }
    byRack.get(rack)!.push(id);
  }
  return order.map((rack) => ({ rack, nodes: byRack.get(rack)! }));
}
