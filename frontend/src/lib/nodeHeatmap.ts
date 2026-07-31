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

export interface HeatmapView {
  id: string;
  label: string;
  /** Metric column this view reads, or null for the up/down view. */
  column: string | null;
  unit: string;
}

/**
 * Available views: node status first, then one per column that produces a number.
 *
 * Built from the fleet payload rather than hardcoded, so a site running different benchmarks
 * gets its own views without a code change.
 */
export function heatmapViews(fleet: BringupFleet | null): HeatmapView[] {
  const views: HeatmapView[] = [
    { id: STATUS_VIEW_ID, label: "Node status (up / down)", column: null, unit: "" },
  ];
  for (const col of fleet?.columns ?? []) {
    const unit = fleet?.metric_labels?.[col];
    if (unit) views.push({ id: col, label: `${col} — ${unit}`, column: col, unit });
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
