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
import { useMemo, useState } from "react";
import { Banner, Button, Card, Flex, SegmentedControl, Select, Text } from "@nvidia/foundations-react-core";
import CircularProgress from "@mui/material/CircularProgress";
import Tooltip from "@mui/material/Tooltip";
import { RefreshCw } from "lucide-react";
import type { BringupFleet } from "../../types/bringup";
import type { NodeInfo } from "../rack/NodeSelector";
import { NodeMetricsTable } from "./NodeMetricsTable";
import {
  STATUS_FILL,
  STATUS_VIEW_ID,
  VERDICT_BORDER,
  borderFor,
  groupByRack,
  heatmapViews,
  metricOf,
  metricRange,
  nodeMetrics,
  paintCell,
  verdictOf,
} from "../../lib/nodeHeatmap";

export interface NodeHeatmapProps {
  fleet: BringupFleet | null;
  /** Node inventory with live up/down state, from the dashboard status feed. */
  nodes: NodeInfo[];
  /** Rack id for a node, so cells can be grouped the way the floor is laid out. */
  rackOf: (nodeId: string) => string;
  loading?: boolean;
  error?: string | null;
  onRefresh?: () => void;
}

// Wide enough for the gauge to read as a proportion rather than a dot.
const CELL_W = 40;
const CELL_H = 20;
const BAR_H = 10;
/** Unfilled part of the gauge; darker than the cell so the fill boundary is visible. */
const GAUGE_TRACK = "#0b0f14";

const HEATMAP_MODE = "heatmap";
const TABLE_MODE = "table";

export function NodeHeatmap({ fleet, nodes, rackOf, loading, error, onRefresh }: NodeHeatmapProps) {
  const views = useMemo(() => heatmapViews(fleet), [fleet]);
  const [viewId, setViewId] = useState(STATUS_VIEW_ID);
  const [mode, setMode] = useState(HEATMAP_MODE);
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
  const rows = useMemo(() => {
    const ids = new Set<string>(nodes.map((n) => n.id));
    for (const n of fleet?.nodes ?? []) ids.add(n.node);
    return groupByRack([...ids].sort(), rackOf);
  }, [nodes, fleet, rackOf]);

  const total = rows.reduce((sum, r) => sum + r.nodes.length, 0);

  return (
    <Card style={{ padding: 16 }}>
      <Flex justify="between" align="center" style={{ marginBottom: 12, gap: 12, flexWrap: "wrap" }}>
        <div>
          <Text kind="title/sm">Node heatmap</Text>
          <Text kind="body/regular/sm" style={{ color: "var(--nv-color-text-secondary)" }}>
            {total} node{total === 1 ? "" : "s"} across {rows.length} rack
            {rows.length === 1 ? "" : "s"}.{" "}
            {view?.column
              ? "Border is this benchmark's verdict; bar length is the value."
              : "Border and bar are node up/down."}{" "}
            Hover a node for all of its metrics.
          </Text>
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
            <Select
              aria-label="Heatmap data view"
              value={view?.id}
              onValueChange={(v: string) => setViewId(v)}
              items={views.map((v) => ({ value: v.id, children: v.label }))}
              size="small"
            />
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

      {loading && !fleet && (
        <Flex align="center" style={{ gap: 8, padding: 24 }}>
          <CircularProgress size={18} />
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

      {total > 0 && mode === HEATMAP_MODE && (
        <>
          <Legend view={view} range={range} />
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
                  {ids.map((id) => {
                    const status = statusById.get(id) ?? "unknown";
                    const node = fleetByNode.get(id);
                    const paint = paintCell(view, node, status, range);
                    const value = node ? metricOf(node, view?.column ?? null) : null;
                    const verdict = node ? verdictOf(node, view?.column ?? null) : "";
                    const detail = !view?.column
                      ? `status ${status.toUpperCase()}`
                      : value !== null
                        ? `${value} ${view.unit} (${verdict || "—"})`
                        : verdict || "no result";
                    return (
                      <Tooltip
                        key={id}
                        title={<CellTooltip node={id} status={status} fleetNode={node} />}
                        arrow
                        placement="top"
                      >
                        <div
                          data-testid={`heatmap-cell-${id}`}
                          aria-label={`${id}: ${detail}`}
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
                          {/* Gauge: an explicit track so a part-filled bar reads as a
                              proportion rather than as a short block of colour. */}
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
                                background: paint.barColor,
                                transition: "width 120ms ease-out",
                              }}
                            />
                          </div>
                        </div>
                      </Tooltip>
                    );
                  })}
                </div>
              </Flex>
            ))}
          </div>
        </>
      )}
    </Card>
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
