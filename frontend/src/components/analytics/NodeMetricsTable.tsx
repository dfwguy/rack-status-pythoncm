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
 * Node-by-node table: one row per node, one column per bring-up benchmark.
 *
 * The companion to the heatmap rather than a replacement. The heatmap answers "where in the
 * cluster is this bad" across hundreds of nodes at once; this answers "what exactly did this node
 * measure", because a gauge alone cannot discriminate values that cluster near the ceiling — a
 * fleet of 600–690 GB/s results is 87–100% of full on every cell. Showing the number solves that,
 * and the gauge behind it keeps the row scannable.
 *
 * Built on Kaizen UI (Table, Pagination, TextInput, Tag) per the KUI-first rule for new code.
 */
import { useCallback, useMemo, useState } from "react";
import {
  Flex,
  Table,
  Tag,
  Text,
  TextInput,
  Tooltip,
} from "@nvidia/foundations-react-core";
import { Link } from "react-router-dom";
import { ArrowDown, ArrowUp, Search } from "lucide-react";
import type { BringupFleet, BringupFleetNode } from "../../types/bringup";
import type { NodeInfo } from "../rack/NodeSelector";
import { MultiSelectCombobox } from "../MultiSelectCombobox";
import {
  GAUGE_GRADIENT,
  GAUGE_TRACK,
  compareMetric,
  gaugeFraction,
  metricOf,
  metricRange,
  scaleTop,
  splitColumns,
  statusRank,
  verdictLabel,
  verdictOf,
  verdictRank,
} from "../../lib/nodeHeatmap";
import { usePagedRows } from "../../hooks/usePagedRows";
import { TablePager } from "../TablePager";

const STATUS_TONE: Record<NodeInfo["status"], "green" | "red" | "yellow" | "gray"> = {
  up: "green",
  down: "red",
  inst: "yellow",
  unknown: "gray",
};

const STATUS_LABEL: Record<NodeInfo["status"], string> = {
  up: "Up",
  down: "Down",
  inst: "Installing",
  unknown: "Unknown",
};

export interface NodeMetricsTableProps {
  fleet: BringupFleet | null;
  nodes: NodeInfo[];
  rackOf: (nodeId: string) => string;
}

const PAGE_SIZE_OPTIONS = [25, 50, 100, 250];
const DEFAULT_PAGE_SIZE = 50;

/** Non-benchmark sort keys. */
const SORT_NODE = "__node";
const SORT_RACK = "__rack";
const SORT_STATUS = "__status";

const STATUS_FILTER_ITEMS = [
  { value: "up", children: "Up" },
  { value: "down", children: "Down" },
  { value: "inst", children: "Installing" },
  { value: "unknown", children: "Unknown" },
];

const VERDICT_FILTER_ITEMS = [
  { value: "Pass", children: "Pass" },
  { value: "Failed", children: "Failed" },
  { value: "Running", children: "Running" },
  { value: "Untested", children: "Untested" },
];

export function NodeMetricsTable({ fleet, nodes, rackOf }: NodeMetricsTableProps) {
  const [sortKey, setSortKey] = useState<string>(SORT_NODE);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [query, setQuery] = useState("");
  const [statusFilters, setStatusFilters] = useState<string[]>([]);
  const [verdictFilters, setVerdictFilters] = useState<string[]>([]);


  const { metric: metricCols, statusOnly } = useMemo(() => splitColumns(fleet), [fleet]);

  // One range per metric column: each benchmark has its own scale, so alltoall is never
  // coloured against all-reduce's ceiling.
  const ranges = useMemo(() => {
    const out = new Map<string, ReturnType<typeof metricRange>>();
    for (const col of metricCols) out.set(col, metricRange(fleet, col));
    return out;
  }, [fleet, metricCols]);

  const statusById = useMemo(() => {
    const map = new Map<string, NodeInfo["status"]>();
    for (const n of nodes) map.set(n.id, n.status);
    return map;
  }, [nodes]);

  const fleetByNode = useMemo(() => {
    const map = new Map<string, BringupFleetNode>();
    for (const n of fleet?.nodes ?? []) map.set(n.node, n);
    return map;
  }, [fleet]);

  const allIds = useMemo(() => {
    const ids = new Set<string>(nodes.map((n) => n.id));
    for (const n of fleet?.nodes ?? []) ids.add(n.node);
    return [...ids].sort();
  }, [nodes, fleet]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return allIds.filter((id) => {
      // One search box over node and rack: an operator types a fragment, not a field name.
      if (needle && !id.toLowerCase().includes(needle) && !rackOf(id).toLowerCase().includes(needle)) {
        return false;
      }
      if (statusFilters.length && !statusFilters.includes(statusById.get(id) ?? "unknown")) {
        return false;
      }
      if (verdictFilters.length) {
        // Matches when ANY benchmark on the node has one of the selected results, which is what
        // "show me the nodes with failures" means in practice.
        const node = fleetByNode.get(id);
        const labels = (node?.jobs ?? []).map((j) => verdictLabel(j.status).label);
        if (!labels.some((l) => verdictFilters.includes(l))) return false;
      }
      return true;
    });
  }, [allIds, query, statusFilters, verdictFilters, statusById, fleetByNode, rackOf]);

  const sorted = useMemo(() => {
    const rows = [...filtered];
    const dir = sortDir === "asc" ? 1 : -1;
    rows.sort((a, b) => {
      if (sortKey === SORT_NODE) return a.localeCompare(b) * dir;
      if (sortKey === SORT_RACK) return (rackOf(a).localeCompare(rackOf(b)) || a.localeCompare(b)) * dir;
      if (sortKey === SORT_STATUS) {
        const d =
          statusRank(statusById.get(a) ?? "unknown") - statusRank(statusById.get(b) ?? "unknown");
        return d !== 0 ? d * dir : a.localeCompare(b);
      }
      const na = fleetByNode.get(a);
      const nb = fleetByNode.get(b);
      if (metricCols.includes(sortKey)) {
        const d = compareMetric(
          na ? metricOf(na, sortKey) : null,
          nb ? metricOf(nb, sortKey) : null,
          sortDir,
        );
        return d !== 0 ? d : a.localeCompare(b);
      }
      const d =
        (verdictRank(na ? verdictOf(na, sortKey) : "") -
          verdictRank(nb ? verdictOf(nb, sortKey) : "")) *
        dir;
      return d !== 0 ? d : a.localeCompare(b);
    });
    return rows;
  }, [filtered, sortKey, sortDir, rackOf, statusById, fleetByNode, metricCols]);

  // The shared pager: one pagination implementation for every table in the app. It clamps the
  // page itself, which is what the filters above need on every keystroke.
  const nodesPage = usePagedRows(sorted, DEFAULT_PAGE_SIZE);
  const visible = nodesPage.visible;
  /**
   * Filters and sorts send the user back to page 1; the hook clamps, this makes it deliberate.
   *
   * useCallback because the column definitions memo closes over it: an inline arrow would be a
   * new value every render and rebuild every column on every keystroke.
   */
  const setPage = nodesPage.setPage;
  const resetPage = useCallback(() => setPage(1), [setPage]);

  /** Column order drives both the headers and each row's cells, so they cannot disagree. */
  const sortKeys = useMemo(
    () => [SORT_NODE, SORT_RACK, SORT_STATUS, ...statusOnly, ...metricCols],
    [statusOnly, metricCols],
  );

  const columns = useMemo(
    () =>
      sortKeys.map((key) => {
        const isMetric = metricCols.includes(key);
        const label =
          key === SORT_NODE ? "Node" : key === SORT_RACK ? "Rack" : key === SORT_STATUS ? "Status" : key;
        const sub = isMetric
          ? `${fleet?.metric_labels?.[key] ?? ""} · 0–${Math.round(scaleTop(ranges.get(key) ?? null))}`
          : "";
        return {
          children: (
            <HeaderLabel
              label={label}
              sub={sub}
              active={sortKey === key}
              direction={sortDir}
              columnKey={key}
            />
          ),
          onColumnSelect: ({ columnIndex }: { columnIndex: number }) => {
            const next = sortKeys[columnIndex];
            if (next === sortKey) {
              setSortDir(sortDir === "asc" ? "desc" : "asc");
            } else {
              setSortKey(next);
              // Descending first on a benchmark: the usual question is "what is the fastest",
              // and a second click gives the slowest.
              setSortDir(metricCols.includes(next) ? "desc" : "asc");
            }
            resetPage();
          },
        };
      }),
    [sortKeys, metricCols, fleet, ranges, sortKey, sortDir, resetPage],
  );

  const rows = useMemo(
    () =>
      visible.map((id) => {
        const status = statusById.get(id) ?? "unknown";
        const node = fleetByNode.get(id);
        return {
          id,
          cells: [
            {
              children: (
                // Straight to this node's bring-up panel and logs: the row tells you a node looks
                // wrong, and the next step should not be manual navigation.
                <Link
                  to={`/compute/nodes?node=${encodeURIComponent(id)}`}
                  title={`Open ${id} in Compute → Nodes`}
                  style={{
                    color: "var(--color-brand, #76b900)",
                    textDecoration: "none",
                    fontFamily: "monospace",
                  }}
                >
                  {id}
                </Link>
              ),
            },
            { children: <Text kind="mono/sm">{rackOf(id)}</Text> },
            {
              children: (
                <Tag color={STATUS_TONE[status]} kind="solid" readOnly density="compact">
                  {STATUS_LABEL[status]}
                </Tag>
              ),
            },
            ...statusOnly.map((col) => {
              const v = verdictLabel(node ? verdictOf(node, col) : "");
              return {
                children: (
                  <Tag color={v.tone} kind="outline" readOnly density="compact">
                    {v.label}
                  </Tag>
                ),
              };
            }),
            ...metricCols.map((col) => ({
              children: (
                <GaugeCell
                  node={node}
                  column={col}
                  unit={fleet?.metric_labels?.[col] ?? ""}
                  range={ranges.get(col) ?? null}
                />
              ),
            })),
          ],
        };
      }),
    [visible, statusById, fleetByNode, rackOf, statusOnly, metricCols, fleet, ranges],
  );

  return (
    <>
      <Flex align="end" style={{ gap: 12, flexWrap: "wrap", marginBottom: 12 }}>
        <div style={{ minWidth: 220 }}>
          <Text
            kind="body/regular/sm"
            style={{ color: "var(--text-color-secondary)", marginBottom: 4, display: "block" }}
          >
            Node or rack
          </Text>
          <TextInput
            value={query}
            onValueChange={(v: string) => {
              setQuery(v);
              resetPage();
            }}
            placeholder="Filter by node or rack…"
            size="small"
            dismissible
            slotStart={<Search size={14} />}
            aria-label="Filter by node or rack name"
          />
        </div>
        <div style={{ minWidth: 190 }}>
          <Text
            kind="body/regular/sm"
            style={{ color: "var(--text-color-secondary)", marginBottom: 4, display: "block" }}
          >
            Node status
          </Text>
          <MultiSelectCombobox
            items={STATUS_FILTER_ITEMS}
            value={statusFilters}
            onValueChange={(v) => {
              setStatusFilters(v);
              resetPage();
            }}
            size="small"
            aria-label="Filter by node status"
          />
        </div>
        <div style={{ minWidth: 190 }}>
          <Text
            kind="body/regular/sm"
            style={{ color: "var(--text-color-secondary)", marginBottom: 4, display: "block" }}
          >
            Any benchmark result
          </Text>
          <MultiSelectCombobox
            items={VERDICT_FILTER_ITEMS}
            value={verdictFilters}
            onValueChange={(v) => {
              setVerdictFilters(v);
              resetPage();
            }}
            size="small"
            aria-label="Filter by benchmark result"
          />
        </div>
        <div style={{ marginLeft: "auto" }}>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            {sorted.length} of {allIds.length} nodes
          </Text>
        </div>
      </Flex>

      <Table
        columns={columns}
        rows={rows}
        density="compact"
        layout="auto"
        hoverableRows
        aria-label="Node benchmark results"
      />

      <TablePager {...nodesPage} noun="nodes" pageSizeOptions={PAGE_SIZE_OPTIONS} />
    </>
  );
}

/** Column header: the name, a sort direction arrow when active, and the unit/scale beneath. */
function HeaderLabel({
  label,
  sub,
  active,
  direction,
  columnKey,
}: {
  label: string;
  sub?: string;
  active: boolean;
  direction: "asc" | "desc";
  columnKey: string;
}) {
  return (
    <span data-column={columnKey}>
      <Flex align="center" style={{ gap: 4 }}>
        <span>{label}</span>
        {active &&
          (direction === "asc" ? (
            <ArrowUp size={12} aria-label="sorted ascending" />
          ) : (
            <ArrowDown size={12} aria-label="sorted descending" />
          ))}
      </Flex>
      {sub && (
        <Text
          kind="body/regular/sm"
          style={{ display: "block", color: "var(--nv-color-text-secondary)", fontWeight: 400 }}
        >
          {sub}
        </Text>
      )}
    </span>
  );
}

/**
 * One measurement: the number, over a gauge filled left to right.
 *
 * A cell with no number shows the verdict instead of a zero-length gauge, because a failure that
 * produced nothing and a genuine 0 are different results.
 */
function GaugeCell({
  node,
  column,
  unit,
  range,
}: {
  node: BringupFleetNode | undefined;
  column: string;
  unit: string;
  range: ReturnType<typeof metricRange>;
}) {
  const value = node ? metricOf(node, column) : null;
  const verdict = node ? verdictOf(node, column) : "";
  const v = verdictLabel(verdict);
  const id = node?.node ?? "?";

  if (value === null) {
    return (
      <span data-testid={`cell-${id}-${column}`}>
        <Tag color={v.tone} kind="outline" readOnly density="compact">
          {v.label}
        </Tag>
      </span>
    );
  }

  const fraction = gaugeFraction(value, range);
  const failed = v.label === "Failed";
  return (
    <Tooltip
      slotContent={`${value} ${unit} — ${v.label}${range ? ` · fleet max ${range.max}` : ""}`}
    >
      <span
        data-testid={`cell-${id}-${column}`}
        aria-label={`${column} ${value} ${unit}, ${v.label}`}
        style={{
          position: "relative",
          display: "block",
          minWidth: 88,
          borderRadius: 3,
          overflow: "hidden",
          background: GAUGE_TRACK,
          // A failure keeps its number but is outlined, so a fast-but-rejected result cannot
          // pass for a good one just because its gauge is long.
          border: failed ? "1px solid #b91c1c" : "1px solid transparent",
        }}
      >
        {/* The gradient spans the whole cell; the unfilled remainder is covered rather than the
            gradient being squeezed into the bar. That is what makes a longer bar end in a darker
            blue instead of every bar looking alike. */}
        <span
          aria-hidden
          style={{ position: "absolute", inset: 0, background: GAUGE_GRADIENT, opacity: 0.92 }}
        />
        <span
          data-testid={`gauge-fill-${id}-${column}`}
          data-fraction={fraction.toFixed(3)}
          aria-hidden
          style={{
            position: "absolute",
            top: 0,
            bottom: 0,
            left: `${Math.round(fraction * 100)}%`,
            right: 0,
            background: GAUGE_TRACK,
          }}
        />
        <Text
          kind="mono/sm"
          style={{
            position: "relative",
            display: "block",
            padding: "2px 6px",
            // Right-aligned: digits line up for comparison (KUI's guidance for numeric columns),
            // and the number always sits over the dark end — the track when the bar is short, deep
            // blue when it is long — so one text colour stays readable either way.
            textAlign: "right",
            color: "#f8fafc",
            textShadow: "0 1px 2px rgba(0,0,0,0.75)",
          }}
        >
          {value}
        </Text>
      </span>
    </Tooltip>
  );
}
