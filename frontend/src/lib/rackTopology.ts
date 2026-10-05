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
 * Client-side rack topology helpers for the dashboard "Floor" view.
 *
 * Parses a rack id into a section -> row -> rack hierarchy using the active
 * ``NVIS_RACK_ID`` template (from ``GET /api/status`` ``naming``), and turns the
 * per-rack ``sinfo`` / ``ct_down_unk`` strings into tray-level up/down sets and a
 * rolled-up health verdict. Generation-agnostic: it derives structure from the
 * template's digit slots, so it works for ``MEL01-HC07-SVR-CN0103`` (hall/row/rack)
 * and Bright ``p5-r03`` (pod/rack) alike.
 */
import type { NvisNamingSchema } from "../types/naming";
import type { RackRow, RackHierarchy } from "../types/api";

/** A rack id decomposed against the naming template. */
export interface RackParts {
  /** Grouping key for the section (e.g. ``MEL01-HC07``); "" when the template has < 3 slots. */
  sectionKey: string;
  /** Human label for the section header. */
  sectionLabel: string;
  /** Stable grouping key for the row within its section. */
  rowKey: string;
  /** Short row label (e.g. ``01``). */
  rowLabel: string;
  /** Short rack label (the final slot value, e.g. ``03``). */
  rackLabel: string;
  /** Ordered numeric slot values, for sorting. */
  slots: number[];
}

// Mirrors the backend DSL (nvis_id_templates.py): variable digits ``[\d+]``,
// fixed two-digit ``[NN-NN]``, and single-letter row slots ``[A-Z]`` / ``[a-z]``.
// NOTE: the backend ``hierarchy`` payload is the source of truth; this client
// parser is only a fallback for older backends, but it must stay alpha-aware so
// row-letter sites (e.g. Boston ``bos01-a05``) still group into rows a/b.
const TOKEN_RE = /\[\\d\+\]|\[\d{2}-\d{2}\]|\[[A-Za-z]-[A-Za-z]\]/g;

interface TemplateSeg {
  kind: "lit" | "slot";
  text: string; // literal text (lit) or "" (slot)
  /** For slots: "digits" => [\d+]; "range2" => [NN-NN]; "alpha" => single letter [A-Z]. */
  slot?: "digits" | "range2" | "alpha";
}

function slotKindFor(match: string): "digits" | "range2" | "alpha" {
  if (match === "[\\d+]") return "digits";
  if (/^\[[A-Za-z]-[A-Za-z]\]$/.test(match)) return "alpha";
  return "range2";
}

function parseTemplateSegments(template: string): TemplateSeg[] {
  const segs: TemplateSeg[] = [];
  let pos = 0;
  template.replace(TOKEN_RE, (match: string, offset: number) => {
    if (offset > pos) segs.push({ kind: "lit", text: template.slice(pos, offset) });
    segs.push({ kind: "slot", text: "", slot: slotKindFor(match) });
    pos = offset + match.length;
    return match;
  });
  if (pos < template.length) segs.push({ kind: "lit", text: template.slice(pos) });
  return segs;
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function templateToRegex(segs: TemplateSeg[]): RegExp {
  // Fixed two-digit slots must match exactly two digits, otherwise adjacent
  // slots (e.g. row+rack in ``...[01-99][01-99]``) are greedily mis-split.
  const body = segs
    .map((s) => {
      if (s.kind === "lit") return escapeRe(s.text);
      if (s.slot === "alpha") return "([A-Za-z])";
      if (s.slot === "range2") return "(\\d{2})";
      return "(\\d+)";
    })
    .join("");
  return new RegExp(`^${body}$`, "i");
}

/** Rebuild the template literal/slot string up to and including slot index ``upTo`` with ``values``. */
function rebuildThroughSlot(segs: TemplateSeg[], values: string[], upTo: number): string {
  let out = "";
  let slotI = 0;
  for (const seg of segs) {
    if (seg.kind === "lit") {
      out += seg.text;
    } else {
      if (slotI > upTo) break;
      out += values[slotI] ?? "";
      slotI += 1;
      if (slotI > upTo) break; // stop right after the last wanted slot (drop trailing literal)
    }
  }
  return out.replace(/[-_/:.]+$/, "");
}

/**
 * Decompose ``rackId`` using the rack template. Falls back to splitting on the
 * trailing digit groups when the template is missing or doesn't match.
 */
export function parseRackParts(rackId: string, rackTemplate?: string): RackParts {
  const id = (rackId || "").trim();
  let segs: TemplateSeg[] = [];
  let values: string[] | null = null;

  if (rackTemplate) {
    segs = parseTemplateSegments(rackTemplate);
    const m = templateToRegex(segs).exec(id);
    if (m) values = m.slice(1);
  }

  // Fallback: leading non-digit prefix + each digit group as a slot.
  if (!values) {
    const groups = id.match(/\d+/g) || [];
    values = groups;
    const firstDigit = id.search(/\d/);
    const prefix = firstDigit > 0 ? id.slice(0, firstDigit) : "";
    segs = [];
    if (prefix) segs.push({ kind: "lit", text: prefix });
    groups.forEach((_g, i) => {
      segs.push({ kind: "slot", text: "" });
      if (i < groups.length - 1) segs.push({ kind: "lit", text: "-" });
    });
  }

  // Numeric slots sort by value; single-letter (row) slots sort by char ordinal
  // so row ``a`` precedes ``b`` (matches the backend rack_sort_key).
  const slots = values
    .map((v) => {
      const num = parseInt(v, 10);
      if (!Number.isNaN(num)) return num;
      return v.length === 1 ? v.toLowerCase().charCodeAt(0) : NaN;
    })
    .filter((n) => !Number.isNaN(n));
  const n = values.length;
  const rackLabel = n >= 1 ? values[n - 1] : id;
  const rowLabel = n >= 2 ? values[n - 2] : "";
  const rowKey = n >= 2 ? rebuildThroughSlot(segs, values, n - 2) : id;
  const sectionKey = n >= 3 ? rebuildThroughSlot(segs, values, n - 3) : "";
  const sectionLabel = sectionKey || (n >= 2 ? "Cluster" : id);

  return { sectionKey, sectionLabel, rowKey, rowLabel, rackLabel, slots };
}

// ---------------------------------------------------------------------------
// sinfo / ct_down_unk parsing
// ---------------------------------------------------------------------------

const BAD_STATES = new Set([
  "down",
  "drain",
  "drng",
  "draining",
  "drained",
  "fail",
  "failg",
  "failing",
  "maint",
  "unk",
  "unknown",
  "error",
  "err",
  "nr",
  "no_respond",
  "boot",
  "pow_down",
  "powering_down",
  // Provider (BCM/pythoncm) node states from ds: a node still installing is not "up".
  "inst",
]);

/** Parse ``"idle=17,down=1"`` into ``{idle:17, down:1}``. */
export function parseSinfo(sinfo: string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const part of (sinfo || "").split(/[,\s]+/)) {
    const m = /^([a-z_]+)=(\d+)$/i.exec(part.trim());
    if (m) out[m[1].toLowerCase()] = parseInt(m[2], 10);
  }
  return out;
}

function expandRanges(inner: string): number[] {
  const out: number[] = [];
  for (const part of inner.split(",")) {
    const p = part.trim();
    if (!p) continue;
    if (p.includes("-")) {
      const [a, b] = p.split("-", 2);
      const lo = parseInt(a, 10);
      const hi = parseInt(b, 10);
      if (!Number.isNaN(lo) && !Number.isNaN(hi) && lo <= hi) {
        for (let i = lo; i <= hi; i += 1) out.push(i);
      }
    } else {
      const v = parseInt(p, 10);
      if (!Number.isNaN(v)) out.push(v);
    }
  }
  return out;
}

export interface TrayState {
  up: Set<number>;
  down: Set<number>;
  unk: Set<number>;
}

/**
 * Parse ``ct_down_unk`` such as ``"DN:rack[34];UP:rack[11-18,28-33,35-37]"`` into
 * per-tray up/down/unknown sets. Returns null when there is no per-tray detail.
 */
export function parseTrayState(ctDownUnk: string): TrayState | null {
  const s = (ctDownUnk || "").trim();
  if (!s || s === "-") return null;
  const state: TrayState = { up: new Set(), down: new Set(), unk: new Set() };
  let matched = false;
  const segRe = /(UP|DN|DOWN|UNK|UK)\s*:[^[\]]*\[([^\]]*)\]/gi;
  let m: RegExpExecArray | null;
  while ((m = segRe.exec(s)) !== null) {
    matched = true;
    const kind = m[1].toUpperCase();
    const trays = expandRanges(m[2]);
    const target = kind === "UP" ? state.up : kind === "UNK" || kind === "UK" ? state.unk : state.down;
    trays.forEach((t) => target.add(t));
  }
  return matched ? state : null;
}

export type RackHealth = "healthy" | "partial" | "down" | "unknown";

export interface RackHealthInfo {
  health: RackHealth;
  total: number;
  upCount: number;
  downCount: number;
  /** Ordered tray numbers with per-tray status, when tray detail is available. */
  trays: { tray: number; status: "up" | "down" | "unk" }[];
}

/** Roll a rack row up into a health verdict + (optional) ordered tray list. */
export function rackHealth(row: RackRow): RackHealthInfo {
  const tray = parseTrayState(row.ct_down_unk);
  if (tray) {
    const all = Array.from(new Set([...tray.up, ...tray.down, ...tray.unk])).sort((a, b) => a - b);
    const trays = all.map((t) => ({
      tray: t,
      status: tray.down.has(t) ? ("down" as const) : tray.up.has(t) ? ("up" as const) : ("unk" as const),
    }));
    const downCount = tray.down.size;
    const upCount = tray.up.size;
    const total = all.length;
    const health: RackHealth =
      downCount === 0 && tray.unk.size === 0
        ? "healthy"
        : upCount === 0
          ? "down"
          : "partial";
    return { health, total, upCount, downCount, trays };
  }

  // No tray detail — fall back to sinfo counts.
  const counts = parseSinfo(row.sinfo);
  let bad = 0;
  let total = 0;
  for (const [k, v] of Object.entries(counts)) {
    total += v;
    if (BAD_STATES.has(k)) bad += v;
  }
  if (total === 0) return { health: "unknown", total: 0, upCount: 0, downCount: 0, trays: [] };
  const health: RackHealth = bad === 0 ? "healthy" : bad >= total ? "down" : "partial";
  return { health, total, upCount: total - bad, downCount: bad, trays: [] };
}

/**
 * Columns for the proportional node bar (the no-per-tray-detail case). Rows of 8 when the count is
 * a multiple of 8 (the common GPU rack — 16 -> 2x8, 24 -> 3x8); otherwise ~2 balanced rows capped
 * at 9 wide, so an off-multiple count wraps tidily instead of becoming one long thin strip
 * (NC's 18 -> 2x9).
 */
export function barColumns(cells: number): number {
  if (cells <= 0) return 1;
  if (cells % 8 === 0) return 8;
  return Math.min(9, Math.max(1, Math.ceil(cells / 2)));
}

// ---------------------------------------------------------------------------
// Grouping
// ---------------------------------------------------------------------------

export interface FloorRack {
  row: RackRow;
  parts: RackParts;
  health: RackHealthInfo;
}

export interface FloorRow {
  key: string;
  label: string;
  racks: FloorRack[];
}

export interface FloorSection {
  key: string;
  label: string;
  rows: FloorRow[];
  rackCount: number;
}

/** One Scalable Unit as the floor view draws it. */
export interface FloorScalableUnit {
  id: string;
  label: string;
  racks: FloorRack[];
  rackCount: number;
}

/**
 * Group racks by Scalable Unit, using the mapping the backend sent.
 *
 * A separate axis from section -> row, not a level inside it, because an SU genuinely does
 * not follow the floor: Nscale TX numbers SU1-SU14 across two `dh` rows, so nesting rows
 * under SUs (or the reverse) would misdraw one of them. Both groupings are real — a row is
 * where the racks physically are, an SU is which fabric they share — so the view offers
 * each rather than picking.
 *
 * Racks the mapping does not mention are collected into a trailing "Unassigned" group. That
 * is a genuine state during bring-up, when a rack is on the floor before the SU map catches
 * up, and hiding it would make the floor view quietly disagree with the rack count.
 */
export function buildScalableUnitLayout(
  rows: RackRow[],
  units: Array<{ id: string; label: string; racks: string[] }>,
  naming?: NvisNamingSchema,
): FloorScalableUnit[] {
  // No map means "this cluster has no SU model", so there is no SU grouping to draw --
  // not one Unassigned group holding everything. Same rule as `derive_scalable_units`,
  // which returns [] rather than inventing a single SU.
  if (units.length === 0) return [];

  const tpl = naming?.rack_id;
  const byRack = new Map<string, FloorRack>();
  for (const row of rows) {
    byRack.set(row.rack.toLowerCase(), {
      row,
      parts: parseRackParts(row.rack, tpl),
      health: rackHealth(row),
    });
  }

  const out: FloorScalableUnit[] = [];
  const claimed = new Set<string>();
  for (const unit of units) {
    const racks: FloorRack[] = [];
    for (const rack of unit.racks) {
      const key = rack.toLowerCase();
      const found = byRack.get(key);
      if (found && !claimed.has(key)) {
        racks.push(found);
        claimed.add(key);
      }
    }
    if (racks.length === 0) continue;
    racks.sort((a, b) => slotCmp(a.parts.slots, b.parts.slots));
    out.push({ id: unit.id, label: unit.label || unit.id, racks, rackCount: racks.length });
  }

  const leftover = [...byRack.entries()]
    .filter(([key]) => !claimed.has(key))
    .map(([, value]) => value)
    .sort((a, b) => slotCmp(a.parts.slots, b.parts.slots));
  if (leftover.length > 0) {
    out.push({
      id: "__unassigned",
      label: "Unassigned",
      racks: leftover,
      rackCount: leftover.length,
    });
  }
  return out;
}

function slotCmp(a: number[], b: number[]): number {
  const len = Math.max(a.length, b.length);
  for (let i = 0; i < len; i += 1) {
    const d = (a[i] ?? -1) - (b[i] ?? -1);
    if (d !== 0) return d;
  }
  return 0;
}

/** Group rack rows into section -> row -> rack, sorted numerically by slot. */
export function buildFloorLayout(rows: RackRow[], naming?: NvisNamingSchema): FloorSection[] {
  const tpl = naming?.rack_id;
  const sectionMap = new Map<string, FloorSection>();
  const rowMap = new Map<string, FloorRow>();

  for (const row of rows) {
    const parts = parseRackParts(row.rack, tpl);
    const fr: FloorRack = { row, parts, health: rackHealth(row) };

    let section = sectionMap.get(parts.sectionKey);
    if (!section) {
      section = { key: parts.sectionKey, label: parts.sectionLabel, rows: [], rackCount: 0 };
      sectionMap.set(parts.sectionKey, section);
    }
    const rowKey = `${parts.sectionKey}|${parts.rowKey}`;
    let floorRow = rowMap.get(rowKey);
    if (!floorRow) {
      floorRow = { key: rowKey, label: parts.rowLabel, racks: [] };
      rowMap.set(rowKey, floorRow);
      section.rows.push(floorRow);
    }
    floorRow.racks.push(fr);
    section.rackCount += 1;
  }

  const sections = Array.from(sectionMap.values());
  for (const s of sections) {
    for (const r of s.rows) r.racks.sort((a, b) => slotCmp(a.parts.slots, b.parts.slots));
    s.rows.sort((a, b) => slotCmp(a.racks[0]?.parts.slots ?? [], b.racks[0]?.parts.slots ?? []));
  }
  sections.sort((a, b) => slotCmp(a.rows[0]?.racks[0]?.parts.slots ?? [], b.rows[0]?.racks[0]?.parts.slots ?? []));
  return sections;
}

/**
 * Map rack id -> datacenter row label, from the **backend** ``hierarchy``.
 *
 * Use this for any row grouping or row filter. The row label is whatever the active
 * ``NVIS_RACK_ID`` template calls that slot (``d`` for ``ink50-dh01-d16``, ``a`` for
 * ``bos01-a09``), so it stays correct per site without the SPA parsing rack ids.
 * Returns an empty map when the response predates ``hierarchy`` — callers should treat
 * "no rows known" as "offer no row filter" rather than falling back to a guess.
 */
export function buildRackRowMap(data: { hierarchy?: RackHierarchy } | null): Map<string, string> {
  const map = new Map<string, string>();
  const hierarchy = data?.hierarchy;
  if (!hierarchy) return map;
  for (const section of hierarchy.sections) {
    for (const row of section.rows) {
      for (const hr of row.racks) {
        map.set(hr.rack, row.label || "");
      }
    }
  }
  return map;
}

/**
 * Build the floor layout from the **backend** ``hierarchy`` payload (the single
 * source of truth). Rack ids are joined back to the flat ``rows`` for per-rack
 * status/health; grouping/labels/order come straight from the server so the SPA
 * never re-derives the datacenter structure. Prefer this over ``buildFloorLayout``
 * whenever ``hierarchy`` is present in the status response.
 */
export function floorLayoutFromHierarchy(rows: RackRow[], hierarchy: RackHierarchy): FloorSection[] {
  const byRack = new Map<string, RackRow>();
  for (const r of rows) byRack.set(r.rack, r);

  return hierarchy.sections.map((section) => {
    let rackCount = 0;
    const floorRows: FloorRow[] = section.rows.map((row) => {
      const racks: FloorRack[] = row.racks.map((hr) => {
        const rackRow = byRack.get(hr.rack) ?? ({
          rack: hr.rack,
          nemo: "",
          last_job: "",
          sinfo: "",
          ct_down_unk: "",
        } as RackRow);
        const parts: RackParts = {
          sectionKey: section.key,
          sectionLabel: section.label,
          rowKey: row.key,
          rowLabel: row.label,
          rackLabel: hr.label,
          slots: [],
        };
        return { row: rackRow, parts, health: rackHealth(rackRow) };
      });
      rackCount += racks.length;
      return { key: `${section.key}|${row.key}`, label: row.label, racks };
    });
    return { key: section.key, label: section.label, rows: floorRows, rackCount };
  });
}

export const HEALTH_COLOR: Record<RackHealth, string> = {
  healthy: "var(--color-green-500, #2e7d32)",
  partial: "var(--color-yellow-500, #f9a825)",
  down: "var(--color-red-500, #c62828)",
  unknown: "var(--color-gray-500, #9e9e9e)",
};
