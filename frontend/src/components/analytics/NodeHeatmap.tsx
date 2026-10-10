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
 * Cluster node heatmap: one square per node, grouped by rack.
 *
 * The selected view drives each square's fill; node up/down state is always the border, so the
 * two aggregate into one picture instead of forcing a toggle between them.
 *
 * A CSS grid rather than Chart.js: the cells are a fixed small size, need per-cell tooltips and
 * a two-channel encoding (fill + border), and this is how the existing NVLoom/Clusterkit
 * heatmaps in this codebase are built.
 */
import { useCallback, useMemo, useState } from "react";
import {
  Banner,
  Button,
  Card,
  Flex,
  SegmentedControl,
  Select,
  Spinner,
  Switch,
  Text,
  Tooltip,
} from "@nvidia/foundations-react-core";
import { Download, RefreshCw } from "lucide-react";
import type { BringupFleet } from "../../types/bringup";
import type { NodeInfo } from "../rack/NodeSelector";
import { NodeMetricsTable } from "./NodeMetricsTable";
import {
  COMPACT_NODES_PER_ROW,
  COMPACT_RACKS_PER_ROW,
  COMPLETION_FILL,
  GAUGE_GRADIENT,
  GAUGE_TRACK,
  STATUS_FILL,
  STATUS_VIEW_ID,
  TONE_FILL,
  VERDICT_BORDER,
  borderFor,
  cellFill,
  cellTone,
  chunkNodes,
  groupByRack,
  groupRacksByRow,
  heatmapViews,
  metricOf,
  metricRange,
  nodeMetrics,
  paintCell,
  shortNodeLabel,
  shortRackLabel,
  throughputColor,
  toneCounts,
  verdictOf,
} from "../../lib/nodeHeatmap";
import {
  type HeatmapImageModel,
  type ImageFormat,
  downloadHeatmapImage,
  readableOn,
} from "../../lib/heatmapImage";

export interface NodeHeatmapProps {
  fleet: BringupFleet | null;
  /** Node inventory with live up/down state, from the dashboard status feed. */
  nodes: NodeInfo[];
  /** Rack id for a node, so cells can be grouped the way the floor is laid out. */
  rackOf: (nodeId: string) => string;
  loading?: boolean;
  error?: string | null;
  onRefresh?: () => void;
  /** Cluster name, for the exported image's caption and filename. */
  clusterName?: string;
  /**
   * Draw only the nodes in ``nodes``. Set when ``nodes`` is the server's inventory: bring-up then
   * also knows nodes Slurm defines but that never came up (NC: whole SUs not yet built), and with
   * no placement for them the only way to draw them would be to invent racks. They are counted
   * instead. Unset, bring-up-only nodes are drawn too, so a legacy feed does not under-report.
   */
  onlyListedNodes?: boolean;
}

// Wide enough for the gauge to read as a proportion rather than a dot.
const CELL_W = 40;
const CELL_H = 20;
const BAR_H = 10;

/**
 * Compact cells carry a slot number instead of a gauge.
 *
 * Sized so a two-digit tray number is comfortably readable rather than merely present — the
 * number is the whole point of labelling the square, and at 18px/8pt it was decoration.
 */
const COMPACT_CELL = 30;
const COMPACT_FONT = 15;
/** Rack and row headings, sized to sit with the panel's own body text rather than under it. */
const COMPACT_RACK_FONT = 13;

const HEATMAP_MODE = "heatmap";
const TABLE_MODE = "table";

const COMPACT_LAYOUT = "compact";
const LINEAR_LAYOUT = "linear";

export function NodeHeatmap({
  fleet,
  nodes,
  rackOf,
  loading,
  error,
  onRefresh,
  clusterName,
  onlyListedNodes = false,
}: NodeHeatmapProps) {
  const views = useMemo(() => heatmapViews(fleet), [fleet]);
  const [viewId, setViewId] = useState(STATUS_VIEW_ID);
  const [mode, setMode] = useState(HEATMAP_MODE);
  // Compact by default: the whole floor on one screen is the reason this view exists. Linear
  // trades that for the gauge, which needs a cell too wide to tile.
  const [layout, setLayout] = useState(COMPACT_LAYOUT);
  const [format, setFormat] = useState<ImageFormat>("png");
  const [exportError, setExportError] = useState<string | null>(null);
  // A column can disappear when the site changes its benchmark set; fall back rather than
  // rendering an empty grid against a view that no longer exists.
  const view = views.find((v) => v.id === viewId) ?? views[0];
  const range = useMemo(() => metricRange(fleet, view?.column ?? null), [fleet, view]);

  const statusById = useMemo(() => {
    const map = new Map<string, NodeInfo["status"]>();
    for (const n of nodes) map.set(n.id, n.status);
    return map;
  }, [nodes]);

  const fleetByNode = useMemo(() => {
    const map = new Map<string, BringupFleet["nodes"][number]>();
    for (const n of fleet?.nodes ?? []) map.set(n.node, n);
    return map;
  }, [fleet]);

  // Union of both feeds: a node with results but no inventory entry (or the reverse) still
  // deserves a square, otherwise the map silently under-reports the cluster.
  // Order is the inventory's (racks, then trays); nodes only bring-up knows follow, in natural order.
  const unlisted = useMemo(() => {
    const known = new Set(nodes.map((n) => n.id));
    return (fleet?.nodes ?? []).map((n) => n.node).filter((id) => !known.has(id));
  }, [nodes, fleet]);

  const rows = useMemo(() => {
    const ids: string[] = nodes.map((n) => n.id);
    if (onlyListedNodes) return groupByRack(ids, rackOf);
    const extra = [...unlisted].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
    return groupByRack([...new Set([...ids, ...extra])], rackOf);
  }, [nodes, unlisted, onlyListedNodes, rackOf]);

  /** A compact cell's number: the server's tray when known, else what the id itself carries. */
  const slotOf = useMemo(() => {
    const trays = new Map<string, string>();
    for (const n of nodes) if (n.tray) trays.set(n.id, String(n.tray).padStart(2, "0"));
    return (id: string, rack: string) => trays.get(id) ?? shortNodeLabel(id, rack);
  }, [nodes]);

  const total = rows.reduce((sum, r) => sum + r.nodes.length, 0);

  const legendEntries = useMemo(() => imageLegend(view), [view]);

  const handleDownload = useCallback(async () => {
    if (!view) return;
    setExportError(null);
    const model: HeatmapImageModel = {
      title: `Node heatmap — ${view.label}`,
      subtitle: `${clusterName || "cluster"} · ${total} nodes across ${rows.length} racks · ${new Date().toLocaleString()}`,
      legend: legendEntries,
      racks: rows.map(({ rack, nodes: ids }) => ({
        rack,
        cells: ids.map((id) => {
          const status = statusById.get(id) ?? "unknown";
          const node = fleetByNode.get(id);
          return {
            id,
            fill: cellFill(view, node, status, range),
            border: borderFor(view, node, status),
            label: slotOf(id, rack),
          };
        }),
      })),
    };
    try {
      await downloadHeatmapImage(model, format, clusterName || "cluster");
    } catch (e) {
      setExportError(e instanceof Error ? e.message : String(e));
    }
  }, [view, clusterName, total, rows, legendEntries, statusById, fleetByNode, range, format, slotOf]);

  return (
    <Card style={{ padding: 16 }}>
      <Flex justify="between" align="center" style={{ marginBottom: 12, gap: 12, flexWrap: "wrap" }}>
        <div>
          <Text kind="title/sm" style={{ display: "block", marginBottom: 4 }}>
            Node heatmap
          </Text>
          <Text kind="body/regular/sm" style={{ color: "var(--nv-color-text-secondary)" }}>
            {total} node{total === 1 ? "" : "s"} across {rows.length} rack
            {rows.length === 1 ? "" : "s"}.{" "}
            {view?.column
              ? "Border is this benchmark's verdict; bar length is the value."
              : "Border and bar are node up/down."}{" "}
            Hover a node for all of its metrics.
          </Text>
          {onlyListedNodes && unlisted.length > 0 && (
            <Text
              kind="body/regular/sm"
              data-testid="heatmap-unlisted"
              style={{ display: "block", color: "var(--nv-color-text-secondary)" }}
            >
              {unlisted.length} more node{unlisted.length === 1 ? " has" : "s have"} bring-up results but{" "}
              {unlisted.length === 1 ? "is" : "are"} not in the cluster inventory (defined in Slurm,
              never placed), so {unlisted.length === 1 ? "it is" : "they are"} not drawn.
            </Text>
          )}
        </div>
        <Flex align="center" style={{ gap: 8 }}>
          <SegmentedControl
            aria-label="Node view display"
            value={mode}
            onValueChange={(m: string) => setMode(m)}
            items={[
              { value: HEATMAP_MODE, children: "Heatmap" },
              { value: TABLE_MODE, children: "Table" },
            ]}
            size="small"
          />
          {/* The table shows every benchmark at once, so a single-view selector is only
              meaningful for the heatmap. */}
          {mode === HEATMAP_MODE && (
            <>
              {/* A Switch, not a SegmentedControl: the layout takes effect immediately and
                  there are exactly two states, which is what a switch is for. */}
              <Switch
                slotLabel="Compact"
                size="small"
                checked={layout === COMPACT_LAYOUT}
                onCheckedChange={(on: boolean) => setLayout(on ? COMPACT_LAYOUT : LINEAR_LAYOUT)}
              />
              <Select
                aria-label="Heatmap data view"
                value={view?.id}
                onValueChange={(v: string) => setViewId(v)}
                items={views.map((v) => ({ value: v.id, children: v.label }))}
                size="small"
              />
              <Select
                aria-label="Image format"
                value={format}
                onValueChange={(f: string) => setFormat(f as ImageFormat)}
                items={[
                  { value: "png", children: "PNG" },
                  { value: "jpeg", children: "JPEG" },
                ]}
                size="small"
              />
              <Button
                kind="secondary"
                size="small"
                onClick={() => void handleDownload()}
                disabled={total === 0}
              >
                <Download size={14} style={{ marginRight: 6 }} />
                Download image
              </Button>
            </>
          )}
          {onRefresh && (
            <Button
              kind="secondary"
              size="small"
              onClick={onRefresh}
              disabled={loading}
              aria-label="Refresh heatmap"
            >
              <RefreshCw size={14} />
            </Button>
          )}
        </Flex>
      </Flex>

      {error && (
        <Banner status="error" kind="inline">
          {error}
        </Banner>
      )}

      {exportError && (
        <Banner status="error" kind="inline">
          Could not build the image: {exportError}
        </Banner>
      )}

      {loading && !fleet && (
        <Flex align="center" style={{ gap: 8, padding: 24 }}>
          <Spinner size="small" aria-label="Loading bring-up results" />
          <Text kind="body/regular/sm">Collecting bring-up results for every node…</Text>
        </Flex>
      )}

      {!loading && !error && total === 0 && (
        <Banner status="info" kind="inline">
          No nodes to display yet.
        </Banner>
      )}

      {total > 0 && mode === TABLE_MODE && (
        <NodeMetricsTable fleet={fleet} nodes={nodes} rackOf={rackOf} />
      )}

      {total > 0 && mode === HEATMAP_MODE && view && (
        <>
          <Legend view={view} range={range} />
          {layout === LINEAR_LAYOUT ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 12 }}>
              {rows.map(({ rack, nodes: ids }) => (
                <Flex key={rack} align="center" style={{ gap: 8 }}>
                  <Text
                    kind="mono/sm"
                    style={{
                      minWidth: 150,
                      color: "var(--nv-color-text-secondary)",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {rack}
                  </Text>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 3 }}>
                    {ids.map((id) => (
                      <GaugeCell
                        key={id}
                        id={id}
                        view={view}
                        range={range}
                        status={statusById.get(id) ?? "unknown"}
                        node={fleetByNode.get(id)}
                      />
                    ))}
                  </div>
                </Flex>
              ))}
            </div>
          ) : (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 16,
                marginTop: 12,
                overflowX: "auto",
              }}
            >
              {groupRacksByRow(rows).map(({ row, racks }) => (
                <div key={row || "all"}>
                  {row && (
                    <Text
                      kind="mono/sm"
                      style={{
                        display: "block",
                        marginBottom: 6,
                        fontSize: COMPACT_RACK_FONT + 1,
                        fontWeight: 600,
                      }}
                    >
                      {row}
                    </Text>
                  )}
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: `repeat(${COMPACT_RACKS_PER_ROW}, max-content)`,
                      gap: 10,
                    }}
                  >
                    {racks.map(({ rack, nodes: ids }) => (
                      <RackBlock
                        key={rack}
                        rack={rack}
                        ids={ids}
                        view={view}
                        range={range}
                        statusById={statusById}
                        fleetByNode={fleetByNode}
                        slotOf={slotOf}
                      />
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Card>
  );
}

type FleetNode = BringupFleet["nodes"][number] | undefined;

/** Sentence describing the cell on the current view, for the accessible name and the aria label. */
function cellDetail(
  view: ReturnType<typeof heatmapViews>[number],
  node: FleetNode,
  status: NodeInfo["status"],
): string {
  if (view.kind === "status") return `status ${status.toUpperCase()}`;
  if (view.kind === "completion") return `tests ${cellTone(view, node, status)}`;
  const value = node ? metricOf(node, view.column) : null;
  const verdict = node ? verdictOf(node, view.column) : "";
  if (value !== null) return `${value} ${view.unit} (${verdict || "—"})`;
  return verdict || "no result";
}

/** Wide cell with a load bar: the linear layout, where there is room to show magnitude. */
function GaugeCell({
  id,
  view,
  range,
  status,
  node,
}: {
  id: string;
  view: ReturnType<typeof heatmapViews>[number];
  range: { min: number; max: number } | null;
  status: NodeInfo["status"];
  node: FleetNode;
}) {
  const paint = paintCell(view, node, status, range);
  return (
    <Tooltip slotContent={<CellTooltip node={id} status={status} fleetNode={node} />} side="top">
      <div
        data-testid={`heatmap-cell-${id}`}
        aria-label={`${id}: ${cellDetail(view, node, status)}`}
        style={{
          width: CELL_W,
          height: CELL_H,
          background: paint.cellBg,
          border: `2px solid ${borderFor(view, node, status)}`,
          borderRadius: 4,
          boxSizing: "border-box",
          display: "flex",
          alignItems: "center",
          padding: 2,
        }}
      >
        {/* Gauge: an explicit track so a part-filled bar reads as a proportion rather than as a
            short block of colour. */}
        <div
          style={{
            position: "relative",
            width: "100%",
            height: BAR_H,
            background: GAUGE_TRACK,
            borderRadius: 2,
            overflow: "hidden",
          }}
        >
          <div
            data-testid={`heatmap-bar-${id}`}
            style={{
              position: "absolute",
              inset: 0,
              width: `${Math.round(paint.bar * 100)}%`,
              // Metric views reveal a slice of the shared gradient, so a longer bar ends in a
              // darker blue and neighbouring cells differ even when their values are close. The
              // status view stays a flat red/green, where a gradient would imply a magnitude.
              background: view.column ? GAUGE_GRADIENT : paint.barColor,
              backgroundSize: view.column ? `${CELL_W - 8}px 100%` : undefined,
              backgroundRepeat: "no-repeat",
              transition: "width 120ms ease-out",
            }}
          />
        </div>
      </div>
    </Tooltip>
  );
}

/** Small solid square carrying its slot number: the compact layout. */
function CompactCell({
  id,
  slot,
  view,
  range,
  status,
  node,
}: {
  id: string;
  slot: string;
  view: ReturnType<typeof heatmapViews>[number];
  range: { min: number; max: number } | null;
  status: NodeInfo["status"];
  node: FleetNode;
}) {
  const fill = cellFill(view, node, status, range);
  const label = slot;
  return (
    <Tooltip slotContent={<CellTooltip node={id} status={status} fleetNode={node} />} side="top">
      <div
        data-testid={`heatmap-cell-${id}`}
        aria-label={`${id}: ${cellDetail(view, node, status)}`}
        style={{
          width: COMPACT_CELL,
          height: COMPACT_CELL,
          background: fill,
          border: `2px solid ${borderFor(view, node, status)}`,
          borderRadius: 4,
          boxSizing: "border-box",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontFamily: "var(--nv-font-family-mono, monospace)",
          fontSize: COMPACT_FONT,
          lineHeight: 1,
          color: readableOn(fill),
        }}
      >
        {label}
      </div>
    </Tooltip>
  );
}

/**
 * One rack as a labelled block of trays, with a tally of what its cells are showing.
 *
 * The tally is what makes a tiled floor scannable: colour alone tells you a rack is mixed, the
 * counts tell you whether that means one straggler or half the rack.
 */
function RackBlock({
  rack,
  ids,
  view,
  range,
  statusById,
  fleetByNode,
  slotOf,
}: {
  rack: string;
  ids: string[];
  view: ReturnType<typeof heatmapViews>[number];
  range: { min: number; max: number } | null;
  statusById: Map<string, NodeInfo["status"]>;
  fleetByNode: Map<string, NonNullable<FleetNode>>;
  slotOf: (id: string, rack: string) => string;
}) {
  const counts = toneCounts(
    ids.map((id) => cellTone(view, fleetByNode.get(id), statusById.get(id) ?? "unknown")),
  );
  return (
    <div data-testid={`heatmap-rack-${rack}`}>
      <Flex align="center" style={{ gap: 4, marginBottom: 3 }}>
        {/* Short label, full id on hover: the row heading above already names the prefix. */}
        <Text
          kind="mono/sm"
          title={rack}
          style={{ fontSize: COMPACT_RACK_FONT, color: "var(--nv-color-text-secondary)" }}
        >
          {shortRackLabel(rack)}
        </Text>
        <Flex align="center" style={{ gap: 4, marginLeft: "auto" }}>
          {(["good", "warn", "bad"] as const).map((tone) =>
            counts[tone] > 0 ? (
              <span
                key={tone}
                title={`${counts[tone]} ${tone}`}
                style={{ fontSize: COMPACT_RACK_FONT - 1, fontWeight: 700, color: TONE_FILL[tone] }}
              >
                {counts[tone]}
              </span>
            ) : null,
          )}
        </Flex>
      </Flex>
      <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
        {chunkNodes(ids, COMPACT_NODES_PER_ROW).map((line) => (
          <div key={line[0]} style={{ display: "flex", gap: 3 }}>
            {line.map((id) => (
              <CompactCell
                key={id}
                id={id}
                slot={slotOf(id, rack)}
                view={view}
                range={range}
                status={statusById.get(id) ?? "unknown"}
                node={fleetByNode.get(id)}
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * Everything known about one node, not just the selected view.
 *
 * The selected view answers one question across the fleet; when a cell looks wrong the next
 * question is always "what else is true about this node", and that should not require switching
 * views or opening another page.
 */
function CellTooltip({
  node,
  status,
  fleetNode,
}: {
  node: string;
  status: NodeInfo["status"];
  fleetNode: BringupFleet["nodes"][number] | undefined;
}) {
  const rows = nodeMetrics(fleetNode);
  return (
    <div style={{ minWidth: 220, lineHeight: 1.45 }}>
      <div style={{ fontFamily: "monospace", fontWeight: 700, marginBottom: 2 }}>{node}</div>
      <div style={{ opacity: 0.85, marginBottom: rows.length ? 6 : 0 }}>
        {status.toUpperCase()}
        {fleetNode?.slurm_state ? ` · slurm ${fleetNode.slurm_state}` : ""}
      </div>
      {!rows.length && <div style={{ opacity: 0.85 }}>No bring-up results for this node.</div>}
      {rows.map((r) => (
        <div key={r.name} style={{ display: "flex", gap: 8, justifyContent: "space-between" }}>
          <span style={{ fontFamily: "monospace" }}>{r.name}</span>
          <span style={{ whiteSpace: "nowrap" }}>
            {r.metric !== null ? `${r.metric} ${r.unit}` : "—"}
            <span style={{ opacity: 0.75 }}> · {r.status || "—"}</span>
          </span>
        </div>
      ))}
      {rows.some((r) => r.reason) && (
        <div style={{ marginTop: 6, opacity: 0.85 }}>
          {rows
            .filter((r) => r.reason)
            .map((r) => `${r.name}: ${r.reason}`)
            .join(" · ")}
        </div>
      )}
    </div>
  );
}

function Swatch({ color, label }: { color: string; label: string }) {
  return (
    <Flex align="center" style={{ gap: 5 }}>
      <div style={{ width: 12, height: 12, background: color, borderRadius: 2 }} />
      <Text kind="body/regular/sm" style={{ color: "var(--nv-color-text-secondary)" }}>
        {label}
      </Text>
    </Flex>
  );
}

/**
 * The on-screen legend as plain data, for the exported image.
 *
 * The export has no gradient strip, so a metric view is described by its ends rather than the
 * ramp — the shade of a cell is only meaningful next to a stated scale.
 */
function imageLegend(view: ReturnType<typeof heatmapViews>[number] | undefined) {
  if (!view) return [];
  if (view.kind === "completion") {
    return [
      { label: "all tests done", color: COMPLETION_FILL.all_done },
      { label: "partial", color: COMPLETION_FILL.partial },
      { label: "none run", color: COMPLETION_FILL.none_run },
      { label: "no results", color: COMPLETION_FILL.no_data },
    ];
  }
  if (view.kind === "status") {
    return [
      { label: "up", color: STATUS_FILL.up },
      { label: "down", color: STATUS_FILL.down },
      { label: "installing", color: STATUS_FILL.inst },
      { label: "unknown", color: STATUS_FILL.unknown },
    ];
  }
  return [
    { label: `low ${view.unit}`, color: throughputColor(0.15) },
    { label: `high ${view.unit}`, color: throughputColor(1) },
    { label: "passed (outline)", color: VERDICT_BORDER.PASS },
    { label: "failed (outline)", color: VERDICT_BORDER.FAIL },
    { label: "not run (outline)", color: VERDICT_BORDER.NOT_RUN },
  ];
}

/** An outline swatch, for the border channel. */
function BorderSwatch({ color, label }: { color: string; label: string }) {
  return (
    <Flex align="center" style={{ gap: 5 }}>
      <div
        style={{
          width: 16,
          height: 12,
          border: `2px solid ${color}`,
          borderRadius: 3,
          boxSizing: "border-box",
        }}
      />
      <Text kind="body/regular/sm" style={{ color: "var(--nv-color-text-secondary)" }}>
        {label}
      </Text>
    </Flex>
  );
}

/** States the scale in words, so a colour is never left to interpretation. */
function Legend({
  view,
  range,
}: {
  view: ReturnType<typeof heatmapViews>[number] | undefined;
  range: { min: number; max: number } | null;
}) {
  if (!view) return null;
  if (view.kind === "completion") {
    return (
      <Flex style={{ gap: 14, flexWrap: "wrap" }}>
        <Swatch color={COMPLETION_FILL.all_done} label="all tests done" />
        <Swatch color={COMPLETION_FILL.partial} label="partial" />
        <Swatch color={COMPLETION_FILL.none_run} label="none run" />
        <Swatch color={COMPLETION_FILL.no_data} label="no results" />
        <Text kind="body/regular/sm" style={{ color: "var(--nv-color-text-secondary)" }}>
          · done counts a test that finished, pass or fail; the border is still node up/down
        </Text>
      </Flex>
    );
  }
  if (!view.column) {
    return (
      <Flex style={{ gap: 14, flexWrap: "wrap" }}>
        <Swatch color={STATUS_FILL.up} label="up" />
        <Swatch color={STATUS_FILL.down} label="down" />
        <Swatch color={STATUS_FILL.inst} label="installing" />
        <Swatch color={STATUS_FILL.unknown} label="unknown" />
        <Text kind="body/regular/sm" style={{ color: "var(--nv-color-text-secondary)" }}>
          · pick a benchmark to colour the border by its verdict
        </Text>
      </Flex>
    );
  }
  return (
    <Flex align="center" style={{ gap: 14, flexWrap: "wrap" }}>
      <Flex align="center" style={{ gap: 6 }}>
        <Text kind="body/regular/sm" style={{ color: "var(--nv-color-text-secondary)" }}>
          0
        </Text>
        <div
          style={{
            width: 140,
            height: 12,
            borderRadius: 2,
            background: "linear-gradient(to right, rgb(248,250,252), rgb(96,165,250), rgb(12,42,138))",
          }}
        />
        <Text kind="body/regular/sm" style={{ color: "var(--nv-color-text-secondary)" }}>
          {range ? `${Math.round(range.max)} ${view.unit}` : view.unit}
        </Text>
      </Flex>
      <BorderSwatch color={VERDICT_BORDER.PASS} label="passed" />
      <BorderSwatch color={VERDICT_BORDER.FAIL} label="failed" />
      <BorderSwatch color={VERDICT_BORDER.RETRY} label="retry / incomplete" />
      <BorderSwatch color={VERDICT_BORDER.NOT_RUN} label="not run" />
      <Text kind="body/regular/sm" style={{ color: "var(--nv-color-text-secondary)" }}>
        · an empty bar means no value was recorded
      </Text>
    </Flex>
  );
}
