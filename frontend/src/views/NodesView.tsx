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
import WebTerminalDialog from "../components/rack/WebTerminalDialog";
import { RackCommandProvider } from "../context/RackCommandProvider";
import { useActiveCluster } from "../context/ActiveClusterContext";
import type { RackStatusResponse, RackRow } from "../types/api";

/** One node row in the flattened table: a tray plus the rack/row it belongs to. */
interface NodeTableRow {
  id: string;
  rack: string;
  /** Datacenter row label (from backend hierarchy) — "" when the site has no row level. */
  row: string;
  status: NodeInfo["status"];
  ip: string;
  /** The last_job of the parent rack, forwarded to the command provider. */
  lastJob?: string;
}

/** Used only when no catalog platform/cluster is active to state the rack's real tray count. */
const FALLBACK_TRAYS_PER_RACK = 18;

interface NodesViewProps {
  data: RackStatusResponse | null;
  logDir?: string;
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
 * Compute-node inventory as a flat, filterable table. Reuses the same
 * ``parseNodesFromRack`` helper the Rack Detail and scope tree use, so tray
 * state is parsed once in the SPA. Selecting a node exposes the rack action
 * tabs (Validation / Hardware / Networking / Services) scoped to that node.
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
  const [rowsPerPageChoice, setRowsPerPageChoice] = useState<number | null>(null);
  const [sortAsc, setSortAsc] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(linkedNode || null);
  const [tabIndex, setTabIndex] = useState(0);
  const [parserOpen, setParserOpen] = useState(false);
  const [terminalNode, setTerminalNode] = useState("");

  // A page holds exactly one rack, so the page size is that rack's tray count. Cluster geometry
  // overrides the platform's — the same precedence the backend applies in catalog.context.
  const { activeCluster, activePlatform } = useActiveCluster();
  const traysPerRack =
    activeCluster?.geometry?.trays_per_rack ||
    activePlatform?.geometry?.trays_per_rack ||
    FALLBACK_TRAYS_PER_RACK;
  const rowsPerPage = rowsPerPageChoice ?? traysPerRack;
  const rowsPerPageOptions = useMemo(() => {
    const opts = [1, 2, 4, 8].map((multiple) => traysPerRack * multiple);
    return opts.includes(rowsPerPage) ? opts : [...opts, rowsPerPage].sort((a, b) => a - b);
  }, [traysPerRack, rowsPerPage]);

  const naming = data?.naming;
  const physicalRf0 = useMemo(() => data?.physical_rf0 ?? {}, [data]);
  const rackRowMap = useMemo(() => buildRackRowMap(data), [data]);

  const allNodes = useMemo<NodeTableRow[]>(() => {
    const rows: RackRow[] = data?.rows ?? [];
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
    const set = new Set<string>();
    for (const n of allNodes) set.add(n.rack);
    return [...set]
      .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
      .map((v) => ({ value: v, children: v }));
  }, [allNodes]);

  const filteredNodes = useMemo(() => {
    const q = nameQuery.trim().toLowerCase();
    const rowSet = new Set(rowFilters);
    const rackSet = new Set(rackFilters);
    const out = allNodes.filter((n) => {
      if (rowSet.size > 0 && !rowSet.has(n.row)) return false;
      if (rackSet.size > 0 && !rackSet.has(n.rack)) return false;
      if (q && !n.id.toLowerCase().includes(q)) return false;
      return true;
    });
    out.sort((a, b) => {
      const cmp = a.id.localeCompare(b.id, undefined, { numeric: true });
      return sortAsc ? cmp : -cmp;
    });
    return out;
  }, [allNodes, nameQuery, rowFilters, rackFilters, sortAsc]);

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
        description="Every compute node across the cluster. Filter by row, rack, or name, then select a node to run Validation, Hardware, Networking, and Services actions against it."
      />

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
              <TableCell>
                <TableSortLabel active direction={sortAsc ? "asc" : "desc"} onClick={() => setSortAsc((v) => !v)}>
                  Node
                </TableSortLabel>
              </TableCell>
              <TableCell>Rack</TableCell>
              <TableCell>Row</TableCell>
              <TableCell>Status</TableCell>
              <TableCell>BMC / RedFish</TableCell>
              <TableCell align="right">Terminal</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {pagedNodes.length === 0 && (
              <TableRow>
                <TableCell colSpan={6}>
                  <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                    {allNodes.length === 0 ? "No nodes reported yet." : "No nodes match the current filters."}
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
                    <Text kind="body/regular/sm">{n.rack}</Text>
                  </TableCell>
                  <TableCell>
                    <Text kind="body/regular/sm" style={{ color: n.row ? undefined : "var(--text-color-secondary)" }}>
                      {n.row || "—"}
                    </Text>
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
            setRowsPerPageChoice(parseInt(e.target.value, 10));
            setPage(0);
          }}
          rowsPerPageOptions={rowsPerPageOptions}
        />
      </TableContainer>

      <Box sx={{ mt: 4 }}>
        {!selectedNode && (
          <Banner status="info">Select a node above to run actions against it.</Banner>
        )}

        {selectedNode && (
          <RackCommandProvider rack={selectedNode.rack} logDir={logDir} jobId={selectedNode.lastJob}>
            <Box sx={{ mb: 2 }}>
              <Text kind="title/lg" style={{ display: "block" }}>{selectedNode.id}</Text>
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                Rack {selectedNode.rack}
                {selectedNode.row ? ` · Row ${selectedNode.row}` : ""} · Actions apply to this node only.
              </Text>
            </Box>

            <BringupResultsPanel node={selectedNode.id} logDir={logDir} />

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
                rack={selectedNode.rack}
                logDir={logDir}
                naming={naming}
                selectedNodes={selectedNodeSet}
                disabled={disabled}
                jobId={selectedNode.lastJob}
                onViewResults={() => setParserOpen(true)}
              />
            )}
            {tabIndex === 1 && (
              <HardwareTab
                rack={selectedNode.rack}
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
                rack={selectedNode.rack}
                logDir={logDir}
                selectedNodes={selectedNodeSet}
                disabled={disabled}
              />
            )}
            {tabIndex === 3 && (
              <ServicesTab
                rack={selectedNode.rack}
                logDir={logDir}
                naming={naming}
                selectedNodes={selectedNodeSet}
                disabled={disabled}
              />
            )}

            <ParserResultsDialog
              open={parserOpen}
              onClose={() => setParserOpen(false)}
              rack={selectedNode.rack}
              jobId={selectedNode.lastJob !== "-" ? selectedNode.lastJob : undefined}
              logDir={logDir}
            />
          </RackCommandProvider>
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
