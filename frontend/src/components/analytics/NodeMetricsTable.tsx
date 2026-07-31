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
import Paper from "@mui/material/Paper";
import Tooltip from "@mui/material/Tooltip";
import { Tag, Text } from "@nvidia/foundations-react-core";
import type { BringupFleet, BringupFleetNode } from "../../types/bringup";
import type { NodeInfo } from "../rack/NodeSelector";
import {
  gaugeFraction,
  metricOf,
  metricRange,
  scaleTop,
  splitColumns,
  throughputColor,
  verdictLabel,
  verdictOf,
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

export function NodeMetricsTable({ fleet, nodes, rackOf }: NodeMetricsTableProps) {
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(50);

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

  const maxPage = Math.max(0, Math.ceil(allIds.length / rowsPerPage) - 1);
  const safePage = Math.min(page, maxPage);
  const visible = allIds.slice(safePage * rowsPerPage, safePage * rowsPerPage + rowsPerPage);

  return (
    <>
      <TableContainer component={Paper} variant="outlined">
        <Table size="small" aria-label="Node benchmark results">
          <TableHead>
            <TableRow>
              <TableCell>Node</TableCell>
              <TableCell>Rack</TableCell>
              <TableCell>Status</TableCell>
              {statusOnly.map((col) => (
                <TableCell key={col}>{col}</TableCell>
              ))}
              {metricCols.map((col) => (
                <TableCell key={col} align="center">
                  {col}
                  <Text
                    kind="body/regular/sm"
                    style={{ display: "block", color: "var(--nv-color-text-secondary)" }}
                  >
                    {fleet?.metric_labels?.[col] ?? ""} · 0–
                    {Math.round(scaleTop(ranges.get(col) ?? null))}
                  </Text>
                </TableCell>
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
        count={allIds.length}
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
