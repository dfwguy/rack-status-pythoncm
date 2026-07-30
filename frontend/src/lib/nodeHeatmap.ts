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

/** Fill for a cell with no measurement: a verdict, not a value. */
export const VERDICT_FILL: Record<string, string> = {
  FAIL: "#fecaca",
  NOT_RUN: "#f1f5f9",
  INCOMPLETE: "#fde68a",
  RETRY: "#fde68a",
  RUNNING: "#bfdbfe",
};

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

export interface CellPaint {
  fill: string;
  text: string;
  /** Normalised position in the range, for the metric views. */
  norm: number | null;
}

/**
 * Fill + text colour for one cell.
 *
 * A metric view still has to represent nodes with no number — a FAIL that produced nothing, or
 * a test that never ran. Those get a verdict colour instead of being painted as zero, because
 * "measured 0 GB/s" and "never ran" are different facts and colouring them alike would invent
 * a result.
 */
export function paintCell(
  view: HeatmapView,
  node: BringupFleetNode | undefined,
  status: NodeInfo["status"],
  range: MetricRange | null,
): CellPaint {
  if (!view.column) {
    return { fill: STATUS_FILL[status], text: "#ffffff", norm: null };
  }
  const value = node ? metricOf(node, view.column) : null;
  if (value === null) {
    const verdict = node ? verdictOf(node, view.column) : "";
    return { fill: VERDICT_FILL[verdict] ?? VERDICT_FILL.NOT_RUN, text: "#334155", norm: null };
  }
  // Anchor at zero so cells are comparable in absolute terms; only the top end adapts.
  const top = range && range.max > 0 ? range.max : value || 1;
  const norm = Math.max(0, Math.min(1, value / top));
  return { fill: throughputColor(norm), text: textColorFor(norm), norm };
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
