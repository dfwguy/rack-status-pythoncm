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
import { useState, useMemo, useCallback } from "react";
import { useSearchParams } from "react-router-dom";
import Box from "@mui/material/Box";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TablePagination from "@mui/material/TablePagination";
import TableSortLabel from "@mui/material/TableSortLabel";
import Paper from "@mui/material/Paper";
import TextField from "@mui/material/TextField";
import InputAdornment from "@mui/material/InputAdornment";
import Link from "@mui/material/Link";
import Tabs from "@mui/material/Tabs";
import Tab from "@mui/material/Tab";
import { Banner, Flex, Tag, Text } from "@nvidia/foundations-react-core";
import { MultiSelectCombobox } from "../components/MultiSelectCombobox";
import { Search, Terminal } from "lucide-react";
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import { PageHeader } from "../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { type NodeInfo } from "../components/rack/NodeSelector";
import { parseNodesFromRack } from "../lib/parseNodesFromRack";
import { buildRackRowMap } from "../lib/rackTopology";
import { ValidationTab } from "../components/rack/ValidationTab";
import { HardwareTab } from "../components/rack/HardwareTab";
import { NetworkingTab } from "../components/rack/NetworkingTab";
import { ServicesTab } from "../components/rack/ServicesTab";
import { ParserResultsDialog } from "../components/ParserResultsDialog";
import { BringupResultsPanel } from "../components/bringup/BringupResultsPanel";
import { NodeValidationStages } from "../components/rack/NodeValidationStages";
import WebTerminalDialog from "../components/rack/WebTerminalDialog";
import { RackCommandProvider } from "../context/RackCommandProvider";
import { useActiveCluster } from "../context/ActiveClusterContext";
import type { RackStatusResponse, RackRow } from "../types/api";

/** A discovered compute host with optional rack and datacenter row metadata. */
interface NodeTableRow {
  id: string;
  rack: string | null;
  /** Datacenter row label (from backend hierarchy) — "" when the site has no row level. */
  row: string;
  status: NodeInfo["status"];
  ip: string;
  /** GPUs on this node (scheduler GRES / nvidia-smi sample); undefined when unknown. */
  gpu_count?: number;
  gpu_type?: string;
  /** The last_job of the parent rack, forwarded to the command provider. */
  lastJob?: string;
}

/** Used only when no catalog platform/cluster is active to state the rack's real tray count. */
const FALLBACK_TRAYS_PER_RACK = 18;
const ROWS_PER_PAGE_OPTIONS = [25, 50, 100];

// Keep the filter's unassigned option distinct from a real rack named "unassigned".
const rackFilterValue = (rack: string | null): string => rack === null ? "unassigned" : `rack:${rack}`;

interface NodesViewProps {
  data: RackStatusResponse | null;
  logDir?: string;
}

type NodeSortKey = "node" | "rack" | "row" | "gpus" | "status";

/** Problems first when ascending: a down node is what an operator scanning the list is looking for. */
const STATUS_RANK: Record<NodeInfo["status"], number> = { down: 0, inst: 1, unknown: 2, up: 3 };

const natural = (a: string, b: string) => a.localeCompare(b, undefined, { numeric: true });

/**
 * Order two nodes by a column. A missing value (unassigned rack, no row, no GPU count) sorts last
 * in either direction, so reversing the column never floods the top with blanks, and the node name
 * breaks every tie so the order is stable between polls.
 */
function compareNodes(a: NodeTableRow, b: NodeTableRow, key: NodeSortKey, asc: boolean): number {
  const dir = asc ? 1 : -1;
  const byName = natural(a.id, b.id);
  const ordered = (cmp: number) => (cmp !== 0 ? cmp * dir : byName);
  const withBlanksLast = (blankA: boolean, blankB: boolean, cmp: () => number) => {
    if (blankA !== blankB) return blankA ? 1 : -1;
    return blankA ? byName : ordered(cmp());
  };
  switch (key) {
    case "rack":
      return withBlanksLast(a.rack === null, b.rack === null, () => natural(a.rack ?? "", b.rack ?? ""));
    case "row":
      return withBlanksLast(!a.row, !b.row, () => natural(a.row, b.row));
    case "gpus":
      return withBlanksLast(a.gpu_count === undefined, b.gpu_count === undefined, () => (a.gpu_count ?? 0) - (b.gpu_count ?? 0));
    case "status":
      return ordered(STATUS_RANK[a.status] - STATUS_RANK[b.status]);
    default:
      return ordered(byName);
  }
}

function SortHeader({
  label, sortKey, sort, onSort,
}: {
  label: string;
  sortKey: NodeSortKey;
  sort: { key: NodeSortKey; asc: boolean };
  onSort: (key: NodeSortKey) => void;
}) {
  const active = sort.key === sortKey;
  return (
    <TableCell sortDirection={active ? (sort.asc ? "asc" : "desc") : false}>
      <TableSortLabel active={active} direction={active && !sort.asc ? "desc" : "asc"} onClick={() => onSort(sortKey)}>
        {label}
      </TableSortLabel>
    </TableCell>
  );
}

/**
 * GPUs a node reports against the count its platform is designed for, drawn as a status tag:
 * green when they match, red when they differ, gray when the scheduler reported none. With no
 * platform to compare against it falls back to the plain number.
 */
function GpuCell({ count, expected, type }: { count?: number; expected: number; type?: string }) {
  if (expected <= 0) {
    return (
      <Text
        kind="body/regular/sm"
        style={{ color: count ? undefined : "var(--text-color-secondary)" }}
        title={type || undefined}
      >
        {count ?? "—"}
      </Text>
    );
  }
  const known = typeof count === "number";
  const tone = !known ? "gray" : count === expected ? "green" : "red";
  const detail = known
    ? `${count} GPU${count === 1 ? "" : "s"} reported by the scheduler; the platform design is ${expected}${type ? ` (${type})` : ""}`
    : `No GPU count reported by the scheduler; the platform design is ${expected}`;
  return (
    <Tag color={tone} kind="solid" readOnly density="compact" title={detail} aria-label={detail}>
      {known ? count : "—"}/{expected}
    </Tag>
  );
}

const STATUS_TONE: Record<NodeInfo["status"], "green" | "red" | "yellow" | "gray"> = {
  up: "green",
  down: "red",
  inst: "yellow",
  unknown: "gray",
};

function statusLabel(status: NodeInfo["status"]): string {
  switch (status) {
    case "up":
      return "UP";
    case "down":
      return "DOWN";
    case "inst":
      return "INST";
    default:
      return "UNKNOWN";
  }
}

function redfishHref(ip: string): string | null {
  const h = ip.trim();
  if (!h || /[\s/@#?\\]/.test(h)) return null;
  if (h.startsWith("[")) return `https://${h}`;
  const colons = (h.match(/:/g) || []).length;
  if (colons >= 2 || h.includes("::")) return `https://[${h}]`;
  return `https://${h}`;
}

/**
 * Compute-node inventory as a flat, filterable table, independent of rack membership.
 * Older servers without a nodes inventory retain the legacy rack expansion. Node-scoped
 * actions use the discovered hostname directly when no rack association exists.
 */
export function NodesView({ data, logDir }: NodesViewProps) {
  // Deep link from the Analytics node table: ?node=<id> opens straight to that node. Applied as
  // initial state rather than in an effect — it is where the page starts, not something to sync.
  // The name filter is seeded too, otherwise the row sits on page 30 of a 600-node table.
  const [searchParams] = useSearchParams();
  const linkedNode = searchParams.get("node")?.trim() ?? "";

  const [rowFilters, setRowFilters] = useState<string[]>([]);
  const [rackFilters, setRackFilters] = useState<string[]>([]);
  const [nameQuery, setNameQuery] = useState(linkedNode);
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(25);
  const [sort, setSort] = useState<{ key: NodeSortKey; asc: boolean }>({ key: "node", asc: true });
  const [selectedId, setSelectedId] = useState<string | null>(linkedNode || null);
  const [tabIndex, setTabIndex] = useState(0);
  const [parserOpen, setParserOpen] = useState(false);
  const [terminalNode, setTerminalNode] = useState("");

  // Geometry is used only for compatibility with servers that lack canonical node inventory.
  const { activeCluster, activePlatform } = useActiveCluster();
  const traysPerRack =
    activeCluster?.geometry?.trays_per_rack ||
    activePlatform?.geometry?.trays_per_rack ||
    FALLBACK_TRAYS_PER_RACK;

  // What one node of this cluster's platform is designed to hold; 0 = no platform to compare with.
  const expectedGpus = activePlatform?.geometry?.gpus_per_node || 0;

  const naming = data?.naming;
  const physicalRf0 = useMemo(() => data?.physical_rf0 ?? {}, [data]);
  const rackRowMap = useMemo(() => buildRackRowMap(data), [data]);

  const allNodes = useMemo<NodeTableRow[]>(() => {
    const rows: RackRow[] = data?.rows ?? [];
    if (data?.nodes !== undefined) {
      const racks = new Map(rows.map((r) => [r.rack, r]));
      return (data.nodes ?? []).map((node) => ({
        ...node,
        row: node.rack === null ? "" : rackRowMap.get(node.rack) ?? "",
        lastJob: node.rack === null ? undefined : racks.get(node.rack)?.last_job,
      }));
    }
    const out: NodeTableRow[] = [];
    for (const r of rows) {
      const nodes = parseNodesFromRack(r.rack, r.ct_down_unk, physicalRf0, traysPerRack, naming);
      for (const n of nodes) {
        out.push({
          id: n.id,
          rack: r.rack,
          row: rackRowMap.get(r.rack) ?? "",
          status: n.status,
          ip: n.ip ?? "",
          lastJob: r.last_job,
        });
      }
    }
    return out;
  }, [data, physicalRf0, naming, rackRowMap, traysPerRack]);

  const rowOptions = useMemo(() => {
    const set = new Set<string>();
    for (const n of allNodes) if (n.row) set.add(n.row);
    return [...set]
      .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
      .map((v) => ({ value: v, children: v }));
  }, [allNodes]);

  const rackOptions = useMemo(() => {
    const options = new Map<string, string>();
    for (const n of allNodes) options.set(rackFilterValue(n.rack), n.rack ?? "Unassigned");
    return [...options]
      .sort((a, b) => a[1].localeCompare(b[1], undefined, { numeric: true }))
      .map(([value, children]) => ({ value, children }));
  }, [allNodes]);

  const filteredNodes = useMemo(() => {
    const q = nameQuery.trim().toLowerCase();
    const rowSet = new Set(rowFilters);
    const rackSet = new Set(rackFilters);
    const out = allNodes.filter((n) => {
      if (rowSet.size > 0 && !rowSet.has(n.row)) return false;
      if (rackSet.size > 0 && !rackSet.has(rackFilterValue(n.rack))) return false;
      if (q && !n.id.toLowerCase().includes(q)) return false;
      return true;
    });
    out.sort((a, b) => compareNodes(a, b, sort.key, sort.asc));
    return out;
  }, [allNodes, nameQuery, rowFilters, rackFilters, sort]);

  /** A new column starts ascending; clicking the active one reverses it. Back to page one either way. */
  const sortBy = useCallback((key: NodeSortKey) => {
    setSort((cur) => ({ key, asc: cur.key === key ? !cur.asc : true }));
    setPage(0);
  }, []);

  /**
   * Clamp the page during render rather than in an effect: a live status poll
   * can shrink ``filteredNodes`` below the current page without any user action,
   * and clamping here keeps the slice valid without a cascading setState.
   */
  const maxPage = Math.max(0, Math.ceil(filteredNodes.length / rowsPerPage) - 1);
  const safePage = Math.min(page, maxPage);

  const pagedNodes = useMemo(() => {
    const start = safePage * rowsPerPage;
    return filteredNodes.slice(start, start + rowsPerPage);
  }, [filteredNodes, safePage, rowsPerPage]);

  /** Selection is derived: a node that leaves the inventory simply yields null. */
  const selectedNode = useMemo(
    () => allNodes.find((n) => n.id === selectedId) ?? null,
    [allNodes, selectedId],
  );

  const resetPage = useCallback(() => setPage(0), []);

  const selectedNodeSet = useMemo(
    () => (selectedNode ? new Set([selectedNode.id]) : new Set<string>()),
    [selectedNode],
  );

  const disabled = selectedNode === null;

  return (
    <Box component="section" sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}>
      <PageHeader
        contentId="nodes"
        title="Nodes"
        description="Every discovered compute node, including nodes without a rack. Filter by row, rack, or name, then select a node to view results and available actions."
      />

      {data?.node_inventory_error && (
        <Banner status="warning" style={{ marginBottom: 16 }}>{data.node_inventory_error}</Banner>
      )}

      <Paper variant="outlined" sx={{ p: 2, mb: 3 }}>
        <Flex gap="4" wrap="wrap" align="end">
          <div style={{ minWidth: 220, flex: "1 1 240px" }}>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 4, display: "block" }}>
              Name
            </Text>
            <TextField
              value={nameQuery}
              onChange={(e) => {
                setNameQuery(e.target.value);
                resetPage();
              }}
              placeholder="Filter by node name…"
              size="small"
              slotProps={{
                input: {
                  startAdornment: (
                    <InputAdornment position="start">
                      <Search size={16} />
                    </InputAdornment>
                  ),
                },
              }}
              fullWidth
            />
          </div>

          {rowOptions.length > 0 && (
            <div style={{ minWidth: 180 }}>
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 4, display: "block" }}>
                Row
              </Text>
              <MultiSelectCombobox
                items={rowOptions}
                value={rowFilters}
                onValueChange={(v) => {
                  setRowFilters(v);
                  resetPage();
                }}
                size="small"
                aria-label="Filter by datacenter row"
              />
            </div>
          )}

          <div style={{ minWidth: 220 }}>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 4, display: "block" }}>
              Rack
            </Text>
            <MultiSelectCombobox
              items={rackOptions}
              value={rackFilters}
              onValueChange={(v) => {
                setRackFilters(v);
                resetPage();
              }}
              size="small"
              aria-label="Filter by rack"
            />
          </div>

          <div style={{ marginLeft: "auto" }}>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              {filteredNodes.length} of {allNodes.length} nodes
            </Text>
          </div>
        </Flex>
      </Paper>

      <TableContainer component={Paper} variant="outlined">
        <Table size="small" aria-label="Compute nodes">
          <TableHead>
            <TableRow>
              <SortHeader label="Node" sortKey="node" sort={sort} onSort={sortBy} />
              <SortHeader label="Rack" sortKey="rack" sort={sort} onSort={sortBy} />
              <SortHeader label="Row" sortKey="row" sort={sort} onSort={sortBy} />
              <SortHeader label="GPUs" sortKey="gpus" sort={sort} onSort={sortBy} />
              <SortHeader label="Status" sortKey="status" sort={sort} onSort={sortBy} />
              <TableCell>BMC / RedFish</TableCell>
              <TableCell align="right">Terminal</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {pagedNodes.length === 0 && (
              <TableRow>
                <TableCell colSpan={7}>
                  <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                    {data?.nodes === null
                      ? "Node inventory is unavailable."
                      : allNodes.length === 0 ? "No nodes reported yet." : "No nodes match the current filters."}
                  </Text>
                </TableCell>
              </TableRow>
            )}
            {pagedNodes.map((n) => {
              const isSelected = n.id === selectedId;
              const href = redfishHref(n.ip);
              return (
                <TableRow
                  key={n.id}
                  hover
                  selected={isSelected}
                  onClick={() => setSelectedId(n.id)}
                  sx={{ cursor: "pointer" }}
                >
                  <TableCell>
                    <Text kind="mono/sm">{n.id}</Text>
                  </TableCell>
                  <TableCell>
                    <Text kind="body/regular/sm">{n.rack ?? "Unassigned"}</Text>
                  </TableCell>
                  <TableCell>
                    <Text kind="body/regular/sm" style={{ color: n.row ? undefined : "var(--text-color-secondary)" }}>
                      {n.row || "—"}
                    </Text>
                  </TableCell>
                  <TableCell>
                    <GpuCell count={n.gpu_count} expected={expectedGpus} type={n.gpu_type} />
                  </TableCell>
                  <TableCell>
                    <Tag color={STATUS_TONE[n.status]} kind="solid" readOnly density="compact">
                      {statusLabel(n.status)}
                    </Tag>
                  </TableCell>
                  <TableCell onClick={(e) => e.stopPropagation()}>
                    {href ? (
                      <Link
                        href={href}
                        target="_blank"
                        rel="noopener noreferrer"
                        underline="always"
                        title={`RedFish BMC — ${href}`}
                        sx={{ fontFamily: "ui-monospace, monospace", fontSize: 13 }}
                      >
                        {n.ip}
                      </Link>
                    ) : (
                      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                        {n.ip || "—"}
                      </Text>
                    )}
                  </TableCell>
                  <TableCell align="right" onClick={(e) => e.stopPropagation()}>
                    <Tooltip title={`Open terminal to ${n.id}`}>
                      <IconButton size="small" onClick={() => setTerminalNode(n.id)} aria-label={`Open terminal to ${n.id}`}>
                        <Terminal size={16} />
                      </IconButton>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
        <TablePagination
          component="div"
          count={filteredNodes.length}
          page={safePage}
          onPageChange={(_, p) => setPage(p)}
          rowsPerPage={rowsPerPage}
          onRowsPerPageChange={(e) => {
            setRowsPerPage(parseInt(e.target.value, 10));
            setPage(0);
          }}
          rowsPerPageOptions={ROWS_PER_PAGE_OPTIONS}
        />
      </TableContainer>

      <Box sx={{ mt: 4 }}>
        {!selectedNode && (
          <Banner status="info">Select a node above to run actions against it.</Banner>
        )}

        {selectedNode && (
          <>
            <Box sx={{ mb: 2 }}>
              <Text kind="title/lg" style={{ display: "block" }}>{selectedNode.id}</Text>
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                {selectedNode.rack === null ? "Unassigned" : `Rack ${selectedNode.rack}`}
                {selectedNode.row ? ` · Row ${selectedNode.row}` : ""}
                {" · Actions apply to this node only."}
              </Text>
            </Box>

            <NodeValidationStages node={selectedNode.id} logDir={logDir} />
            <BringupResultsPanel node={selectedNode.id} logDir={logDir} />

            <RackCommandProvider rack={selectedNode.rack ?? selectedNode.id} logDir={logDir} jobId={selectedNode.lastJob}>
              <Tabs
                value={tabIndex}
                onChange={(_, v) => setTabIndex(v)}
                sx={{ borderBottom: 1, borderColor: "divider", mb: 3 }}
              >
                <Tab label="Validation" />
                <Tab label="Hardware" />
                <Tab label="Networking" />
                <Tab label="Services" />
              </Tabs>

              {tabIndex === 0 && (
                <ValidationTab
                  rack={selectedNode.rack ?? ""}
                  logDir={logDir}
                  naming={naming}
                  selectedNodes={selectedNodeSet}
                  disabled={disabled}
                  jobId={selectedNode.lastJob}
                  onViewResults={selectedNode.rack ? () => setParserOpen(true) : undefined}
                />
              )}
              {tabIndex === 1 && (
                <HardwareTab
                  rack={selectedNode.rack ?? ""}
                  logDir={logDir}
                  naming={naming}
                  selectedNodes={selectedNodeSet}
                  disabled={disabled}
                  validationScope="nodes"
                  onSelectPowerResetTargets={() => setSelectedId(selectedNode.id)}
                />
              )}
              {tabIndex === 2 && (
                <NetworkingTab
                  rack={selectedNode.rack ?? ""}
                  logDir={logDir}
                  naming={naming}
                  selectedNodes={selectedNodeSet}
                  disabled={disabled}
                />
              )}
              {tabIndex === 3 && (
                <ServicesTab
                  rack={selectedNode.rack ?? ""}
                  logDir={logDir}
                  naming={naming}
                  selectedNodes={selectedNodeSet}
                  disabled={disabled}
                />
              )}

              {selectedNode.rack && (
                <ParserResultsDialog
                  open={parserOpen}
                  onClose={() => setParserOpen(false)}
                  rack={selectedNode.rack}
                  jobId={selectedNode.lastJob !== "-" ? selectedNode.lastJob : undefined}
                  logDir={logDir}
                />
              )}
            </RackCommandProvider>
          </>
        )}
      </Box>

      <WebTerminalDialog
        open={!!terminalNode}
        onClose={() => setTerminalNode("")}
        target={terminalNode}
        mode="node"
      />
    </Box>
  );
}
