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
import { Banner, Button, Card, Flex, Select, Text } from "@nvidia/foundations-react-core";
import CircularProgress from "@mui/material/CircularProgress";
import Tooltip from "@mui/material/Tooltip";
import { RefreshCw } from "lucide-react";
import type { BringupFleet } from "../../types/bringup";
import type { NodeInfo } from "../rack/NodeSelector";
import {
  STATUS_BORDER,
  STATUS_FILL,
  STATUS_VIEW_ID,
  groupByRack,
  heatmapViews,
  metricOf,
  metricRange,
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

const CELL = 22;

export function NodeHeatmap({ fleet, nodes, rackOf, loading, error, onRefresh }: NodeHeatmapProps) {
  const views = useMemo(() => heatmapViews(fleet), [fleet]);
  const [viewId, setViewId] = useState(STATUS_VIEW_ID);
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
            {rows.length === 1 ? "" : "s"}. Border shows up/down on every view.
          </Text>
        </div>
        <Flex align="center" style={{ gap: 8 }}>
          <Select
            aria-label="Heatmap data view"
            value={view?.id}
            onValueChange={(v: string) => setViewId(v)}
            items={views.map((v) => ({ value: v.id, children: v.label }))}
            size="small"
          />
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

      {total > 0 && (
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
                    const detail =
                      view?.column === null || view === undefined
                        ? `status ${status.toUpperCase()}`
                        : value !== null
                          ? `${value} ${view.unit} (${verdict || "—"})`
                          : verdict || "no result";
                    return (
                      <Tooltip key={id} title={`${id} — ${detail}`} arrow>
                        <div
                          data-testid={`heatmap-cell-${id}`}
                          aria-label={`${id}: ${detail}`}
                          style={{
                            width: CELL,
                            height: CELL,
                            background: paint.fill,
                            border: `2px solid ${STATUS_BORDER[status]}`,
                            borderRadius: 3,
                            boxSizing: "border-box",
                          }}
                        />
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
      <Swatch color="#fecaca" label="failed, no value" />
      <Swatch color="#f1f5f9" label="not run" />
    </Flex>
  );
}
