/**
 * Node-by-node table: one row per node, one column per bring-up benchmark.
 *
 * The companion to the heatmap rather than a replacement. The heatmap answers "where in the
 * cluster is this bad" across hundreds of nodes at once; this answers "what exactly did this node
 * measure", because a gauge alone cannot discriminate values that cluster near the ceiling — a
 * fleet of 600–690 GB/s results is 87–100% of full on every cell. Showing the number solves that,
 * and the gauge behind it keeps the row scannable.
 */
import { useMemo, useState } from "react";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TablePagination from "@mui/material/TablePagination";
import TableRow from "@mui/material/TableRow";
import TableSortLabel from "@mui/material/TableSortLabel";
import TextField from "@mui/material/TextField";
import InputAdornment from "@mui/material/InputAdornment";
import Paper from "@mui/material/Paper";
import Tooltip from "@mui/material/Tooltip";
import { Flex, Tag, Text } from "@nvidia/foundations-react-core";
import { Search } from "lucide-react";
import { MultiSelectCombobox } from "../MultiSelectCombobox";
import type { BringupFleet, BringupFleetNode } from "../../types/bringup";
import type { NodeInfo } from "../rack/NodeSelector";
import {
  compareMetric,
  gaugeFraction,
  metricOf,
  metricRange,
  scaleTop,
  splitColumns,
  statusRank,
  throughputColor,
  verdictLabel,
  verdictOf,
  verdictRank,
} from "../../lib/nodeHeatmap";

const STATUS_TONE: Record<NodeInfo["status"], "green" | "red" | "yellow" | "gray"> = {
  up: "green",
  down: "red",
  inst: "yellow",
  unknown: "gray",
};

export interface NodeMetricsTableProps {
  fleet: BringupFleet | null;
  nodes: NodeInfo[];
  rackOf: (nodeId: string) => string;
}

const ROWS_PER_PAGE_OPTIONS = [25, 50, 100, 250];

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
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(50);
  const [sortKey, setSortKey] = useState<string>(SORT_NODE);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [query, setQuery] = useState("");
  const [statusFilters, setStatusFilters] = useState<string[]>([]);
  const [verdictFilters, setVerdictFilters] = useState<string[]>([]);

  const resetPage = () => setPage(0);

  /** Clicking a header sorts by it; clicking the active one flips direction. */
  const toggleSort = (key: string, defaultDir: "asc" | "desc") => {
    if (sortKey === key) {
      setSortDir(sortDir === "asc" ? "desc" : "asc");
    } else {
      setSortKey(key);
      setSortDir(defaultDir);
    }
    resetPage();
  };

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
        const d = statusRank(statusById.get(a) ?? "unknown") - statusRank(statusById.get(b) ?? "unknown");
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
          verdictRank(nb ? verdictOf(nb, sortKey) : "")) * dir;
      return d !== 0 ? d : a.localeCompare(b);
    });
    return rows;
  }, [filtered, sortKey, sortDir, rackOf, statusById, fleetByNode, metricCols]);

  const maxPage = Math.max(0, Math.ceil(sorted.length / rowsPerPage) - 1);
  const safePage = Math.min(page, maxPage);
  const visible = sorted.slice(safePage * rowsPerPage, safePage * rowsPerPage + rowsPerPage);

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
          <TextField
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              resetPage();
            }}
            placeholder="Filter by node or rack…"
            size="small"
            fullWidth
            slotProps={{
              input: {
                startAdornment: (
                  <InputAdornment position="start">
                    <Search size={16} />
                  </InputAdornment>
                ),
              },
              htmlInput: { "aria-label": "Filter by node or rack name" },
            }}
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

      <TableContainer component={Paper} variant="outlined">
        <Table size="small" aria-label="Node benchmark results">
          <TableHead>
            <TableRow>
              <SortHeader
                label="Node"
                active={sortKey === SORT_NODE}
                direction={sortDir}
                onClick={() => toggleSort(SORT_NODE, "asc")}
              />
              <SortHeader
                label="Rack"
                active={sortKey === SORT_RACK}
                direction={sortDir}
                onClick={() => toggleSort(SORT_RACK, "asc")}
              />
              <SortHeader
                label="Status"
                active={sortKey === SORT_STATUS}
                direction={sortDir}
                onClick={() => toggleSort(SORT_STATUS, "asc")}
              />
              {statusOnly.map((col) => (
                <SortHeader
                  key={col}
                  label={col}
                  active={sortKey === col}
                  direction={sortDir}
                  onClick={() => toggleSort(col, "asc")}
                />
              ))}
              {metricCols.map((col) => (
                <SortHeader
                  key={col}
                  label={col}
                  align="center"
                  // Descending first on a benchmark: the usual question is "what is the fastest",
                  // and a second click gives the slowest.
                  active={sortKey === col}
                  direction={sortDir}
                  onClick={() => toggleSort(col, "desc")}
                  sub={`${fleet?.metric_labels?.[col] ?? ""} · 0–${Math.round(
                    scaleTop(ranges.get(col) ?? null),
                  )}`}
                />
              ))}
            </TableRow>
          </TableHead>
          <TableBody>
            {visible.map((id) => {
              const status = statusById.get(id) ?? "unknown";
              const node = fleetByNode.get(id);
              return (
                <TableRow key={id} hover>
                  <TableCell sx={{ fontFamily: "monospace", whiteSpace: "nowrap" }}>{id}</TableCell>
                  <TableCell sx={{ fontFamily: "monospace", whiteSpace: "nowrap" }}>
                    {rackOf(id)}
                  </TableCell>
                  <TableCell>
                    <Tag color={STATUS_TONE[status]} kind="solid" readOnly density="compact">
                      {status === "up" ? "Up" : status === "down" ? "Down" : status.toUpperCase()}
                    </Tag>
                  </TableCell>
                  {statusOnly.map((col) => {
                    const v = verdictLabel(node ? verdictOf(node, col) : "");
                    return (
                      <TableCell key={col}>
                        <Tag color={v.tone} kind="outline" readOnly density="compact">
                          {v.label}
                        </Tag>
                      </TableCell>
                    );
                  })}
                  {metricCols.map((col) => (
                    <GaugeCell
                      key={col}
                      node={node}
                      column={col}
                      unit={fleet?.metric_labels?.[col] ?? ""}
                      range={ranges.get(col) ?? null}
                    />
                  ))}
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </TableContainer>
      <TablePagination
        component="div"
        count={sorted.length}
        page={safePage}
        onPageChange={(_, p) => setPage(p)}
        rowsPerPage={rowsPerPage}
        onRowsPerPageChange={(e) => {
          setRowsPerPage(parseInt(e.target.value, 10));
          setPage(0);
        }}
        rowsPerPageOptions={ROWS_PER_PAGE_OPTIONS}
      />
    </>
  );
}

/** A sortable column header, with the unit/scale as a subtitle for metric columns. */
function SortHeader({
  label,
  sub,
  align = "left",
  active,
  direction,
  onClick,
}: {
  label: string;
  sub?: string;
  align?: "left" | "center";
  active: boolean;
  direction: "asc" | "desc";
  onClick: () => void;
}) {
  return (
    <TableCell align={align} sortDirection={active ? direction : false}>
      <TableSortLabel active={active} direction={active ? direction : "asc"} onClick={onClick}>
        {label}
      </TableSortLabel>
      {sub && (
        <Text
          kind="body/regular/sm"
          style={{ display: "block", color: "var(--nv-color-text-secondary)" }}
        >
          {sub}
        </Text>
      )}
    </TableCell>
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

  if (value === null) {
    return (
      <TableCell align="center" data-testid={`cell-${node?.node ?? "?"}-${column}`}>
        <Tag color={v.tone} kind="outline" readOnly density="compact">
          {v.label}
        </Tag>
      </TableCell>
    );
  }

  const fraction = gaugeFraction(value, range);
  const failed = v.label === "Failed";
  return (
    <TableCell
      align="center"
      data-testid={`cell-${node?.node ?? "?"}-${column}`}
      sx={{ p: 0.5, minWidth: 104 }}
    >
      <Tooltip title={`${value} ${unit} — ${v.label}${range ? ` · fleet max ${range.max}` : ""}`} arrow>
        <div
          data-testid={`gauge-${node?.node ?? "?"}-${column}`}
          aria-label={`${column} ${value} ${unit}, ${v.label}`}
          style={{
            position: "relative",
            borderRadius: 3,
            overflow: "hidden",
            background: "var(--nv-color-surface-raised, #1f2530)",
            // A failure keeps its number but is outlined, so a fast-but-rejected result cannot
            // pass for a good one just because its gauge is long.
            border: failed ? "1px solid #b91c1c" : "1px solid transparent",
          }}
        >
          <div
            data-testid={`gauge-fill-${node?.node ?? "?"}-${column}`}
            style={{
              position: "absolute",
              inset: 0,
              width: `${Math.round(fraction * 100)}%`,
              background: throughputColor(fraction),
              opacity: 0.9,
            }}
          />
          <Text
            kind="mono/sm"
            style={{
              position: "relative",
              display: "block",
              padding: "3px 6px",
              color: fraction > 0.55 ? "#f8fafc" : "#e6edf3",
              textShadow: "0 1px 2px rgba(0,0,0,0.55)",
            }}
          >
            {value}
          </Text>
        </div>
      </Tooltip>
    </TableCell>
  );
}
