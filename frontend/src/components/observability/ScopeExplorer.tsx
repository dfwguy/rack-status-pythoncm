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
import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import Box from "@mui/material/Box";
import { SidePanel, Button, Flex, Text, StatusIndicator } from "@nvidia/foundations-react-core";
import { SlidersHorizontal } from "lucide-react";
import { ScopeTree } from "./ScopeTree";
import { allAvailableHostIds, selectionSummary, type ScopeCluster, type ScopeNode, type ScopeRack } from "./mockData";
import { useRackStatus } from "../../hooks/useRackStatus";
import { useActiveCluster } from "../../context/ActiveClusterContext";
import { parseNodesFromRack } from "../../lib/parseNodesFromRack";
import type { RackStatusResponse } from "../../types/api";

/** Width of the scope SidePanel; content reserves this much room when open. */
const SCOPE_PANEL_WIDTH = 340;

/**
 * Top offset so the docked panel sits below the app chrome instead of behind it:
 * the sticky MUI AppBar/Toolbar (~64px) + the breadcrumb bar (~41px).
 */
const APP_HEADER_OFFSET = 105;

/**
 * Poll interval for the scope inventory. Matches the backend's rack-status
 * background refresh (30s) — /api/status is served from a shared server-side
 * cache, so this costs a cached read, not a new cluster sweep.
 */
const SCOPE_POLL_MS = 30_000;

/** Empty tree used before the first /api/status response lands. */
const EMPTY_TOPOLOGY: ScopeCluster = { id: "cluster", label: "Cluster", racks: [] };

/**
 * Cluster display name without a second API call: the literal prefix of the
 * ``NVIS_RACK_ID`` template (``ink50-dh[01-99]-[a-z][\d+]`` -> ``ink50-dh``).
 * /api/cluster-overview carries a nicer ``cluster_name`` but costs a live squeue
 * on the head node every poll, so a view that already has it should pass
 * ``clusterLabel`` instead.
 */
function clusterLabelFromNaming(status: RackStatusResponse | null): string {
  const project = (status?.project_name || "").trim();
  if (project) return project;
  const tpl = status?.naming?.rack_id || "";
  const literal = tpl.split("[")[0].replace(/[-_/:.]+$/, "").trim();
  return literal || "Cluster";
}

/** Short tray label: drop the rack prefix (ink50-dh01-h01-11 -> 11) when it matches. */
function trayLabel(rackId: string, hostId: string): string {
  if (!hostId.toLowerCase().startsWith(rackId.toLowerCase())) return hostId;
  const rest = hostId.slice(rackId.length).replace(/^[-_.]/, "");
  return rest || hostId;
}

/**
 * Build the cluster → rack → tray scope tree from a LIVE ``GET /api/status``
 * payload. Rack order and labels come from the backend-authoritative
 * ``hierarchy`` when present; tray up/down state comes from ``ct_down_unk`` +
 * ``physical_rf0`` through ``parseNodesFromRack`` — the exact same helper the
 * Rack Detail view uses, so there is one tray-state parser in the SPA.
 */
function buildScopeCluster(status: RackStatusResponse | null, clusterName?: string, traysPerRack = 18): ScopeCluster {
  const clusterLabel = clusterName || clusterLabelFromNaming(status);
  const rows = status?.rows ?? [];
  if (rows.length === 0 && !status?.hierarchy) {
    return { ...EMPTY_TOPOLOGY, label: clusterLabel };
  }

  const physicalRf0 = status?.physical_rf0 ?? {};
  const naming = status?.naming;
  const byRack = new Map(rows.map((r) => [r.rack, r]));

  /** rack id -> display label, in backend order. */
  const ordered: { id: string; label: string }[] = [];
  const seen = new Set<string>();
  const hierarchy = status?.hierarchy;
  if (hierarchy) {
    for (const section of hierarchy.sections) {
      for (const row of section.rows) {
        for (const hr of row.racks) {
          if (seen.has(hr.rack)) continue;
          seen.add(hr.rack);
          const label = /^SU\d+$/i.test(row.label)
            ? `${row.label.toUpperCase()} / ${hr.rack}`
            : row.label
              ? `${row.label}${hr.label}`
              : hr.label || hr.rack;
          ordered.push({ id: hr.rack, label });
        }
      }
    }
  }
  // Racks the hierarchy did not cover (older backends, unmatched ids).
  for (const r of rows) {
    if (seen.has(r.rack)) continue;
    seen.add(r.rack);
    ordered.push({ id: r.rack, label: r.rack });
  }

  const racks: ScopeRack[] = ordered.map(({ id, label }) => {
    const row = byRack.get(id);
    const nodes: ScopeNode[] = parseNodesFromRack(id, row?.ct_down_unk ?? "", physicalRf0, traysPerRack, naming).map((n) => ({
      id: n.id,
      label: trayLabel(id, n.id),
      // Only trays the scheduler/platform provider reports as UP are selectable. Down / unknown / installing
      // trays render disabled — most of this cluster is down during bring-up.
      available: n.status === "up",
      status: n.status,
      ip: n.ip,
    }));
    return { id, label, nodes };
  });

  return { id: "cluster", label: clusterLabel, racks };
}

export interface LiveScopeTopology {
  topology: ScopeCluster;
  loading: boolean;
  error: string | null;
  /** False when the backend cannot reach the head node (VPN / SSH down). */
  reachable: boolean;
  /** ``updated_at`` from the rack-status payload (backend timestamp string). */
  updatedAt: string;
}

/**
 * Live scope inventory, reusing the SPA's existing data path: the shared
 * ``useRackStatus`` hook against /api/status (server-side cached). No new fetch
 * layer, no second endpoint. Pass ``clusterLabel`` if the caller already holds a
 * nicer name from /api/cluster-overview.
 */
function useLiveScopeTopology(logDir = "", clusterLabel?: string): LiveScopeTopology {
  const { data, loading, error, reachable } = useRackStatus(logDir, SCOPE_POLL_MS);
  const { activeCluster, activePlatform } = useActiveCluster();
  const traysPerRack =
    activeCluster?.geometry?.trays_per_rack || activePlatform?.geometry?.trays_per_rack || 18;
  const topology = useMemo(
    () => buildScopeCluster(data, clusterLabel, traysPerRack),
    [data, clusterLabel, traysPerRack],
  );
  return { topology, loading, error, reachable, updatedAt: data?.updated_at ?? "" };
}

/** Everything a scoped view needs to build a query / Grafana embed for the selection. */
export interface ScopeSelection {
  /** Selected host ids (always a subset of the trays reported UP). */
  hosts: string[];
  /** Rack ids with at least one selected tray — use for rack-level dashboard vars. */
  racks: string[];
  /** Rack ids where every UP tray is selected. */
  fullRacks: string[];
  /** Human summary, same string shown in the header. */
  summary: string;
  /** True when every UP tray in the cluster is selected. */
  allSelected: boolean;
  /** Live inventory state (first load / backend errors / head-node reachability). */
  loading: boolean;
  error: string | null;
  reachable: boolean;
  totalTrays: number;
  totalUp: number;
}

interface ScopeExplorerProps {
  /**
   * Optional topology override (tests / embedding a pre-built tree). When omitted
   * the tree is built LIVE from /api/status — there is no mock fallback.
   */
  topology?: ScopeCluster;
  /** Log directory forwarded to /api/status; "" lets the backend use its default. */
  logDir?: string;
  /**
   * Cluster display name. Defaults to the rack-naming prefix from /api/status
   * (e.g. "ink50-dh01"); pass ``cluster_name`` from /api/cluster-overview
   * (e.g. "NscaleTX-GB300") if the view already fetches it.
   */
  clusterLabel?: string;
  /** Right-aligned status note shown above the content (e.g. live window). */
  statusNote?: string;
  /**
   * Content area renderer. Receives the selected host ids, a human summary, and
   * the full selection/inventory context so each view can scope its data.
   */
  children: (selectedHosts: string[], summary: string, selection: ScopeSelection) => ReactNode;
}

/**
 * Reusable layout for scope-driven views (Observability: Metrics / Logs /
 * Events). The cluster → rack → tray selection tree lives in a right-docked KUI
 * SidePanel that is open by default and collapsible via the "Scope" toggle (or
 * the panel's close button). The content reserves room on the right so nothing
 * hides behind the panel while open, and reflows to full width when collapsed.
 *
 * The tree is LIVE (see ``useLiveScopeTopology``): real racks and real tray
 * up/down state. Trays that are not UP are disabled and are pruned from the
 * selection on every refresh, so a down tray can never be scoped by accident.
 */
export function ScopeExplorer({
  topology: topologyOverride,
  logDir = "",
  clusterLabel,
  statusNote,
  children,
}: ScopeExplorerProps) {
  const live = useLiveScopeTopology(logDir, clusterLabel);
  const topology = topologyOverride ?? live.topology;

  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [scopeOpen, setScopeOpen] = useState(true);
  const initialisedRef = useRef(false);

  const available = useMemo(() => allAvailableHostIds(topology), [topology]);

  // First real payload selects everything that is UP; later payloads only prune
  // trays that went away/down (never re-expand over the operator's choice).
  useEffect(() => {
    if (topology.racks.length === 0) return;
    if (!initialisedRef.current) {
      initialisedRef.current = true;
      setSelected(new Set(available));
      return;
    }
    setSelected((prev) => {
      const availableSet = new Set(available);
      const next = new Set<string>();
      let dropped = false;
      prev.forEach((id) => {
        if (availableSet.has(id)) next.add(id);
        else dropped = true;
      });
      return dropped ? next : prev;
    });
  }, [topology, available]);

  const selectedHosts = useMemo(() => Array.from(selected), [selected]);
  const summary = selectionSummary(topology, selected);

  const totalTrays = useMemo(
    () => topology.racks.reduce((sum, r) => sum + r.nodes.length, 0),
    [topology]
  );

  const selection = useMemo<ScopeSelection>(() => {
    const racks: string[] = [];
    const fullRacks: string[] = [];
    for (const rack of topology.racks) {
      const up = rack.nodes.filter((n) => n.available);
      if (!rack.nodes.some((n) => selected.has(n.id))) continue;
      racks.push(rack.id);
      if (up.length > 0 && up.every((n) => selected.has(n.id))) fullRacks.push(rack.id);
    }
    return {
      hosts: selectedHosts,
      racks,
      fullRacks,
      summary,
      allSelected: available.length > 0 && selected.size === available.length,
      loading: live.loading,
      error: live.error,
      reachable: live.reachable,
      totalTrays,
      totalUp: available.length,
    };
  }, [topology, selected, selectedHosts, summary, available, live.loading, live.error, live.reachable, totalTrays]);

  // Forwarded to the SidePanelContent element (.nv-side-panel-content): sets the
  // panel width and offsets it below the app header so its heading/close button
  // are not hidden behind the sticky navbar.
  const panelStyle = {
    "--side-panel-width": `${SCOPE_PANEL_WIDTH}px`,
    top: APP_HEADER_OFFSET,
    height: `calc(100vh - ${APP_HEADER_OFFSET}px)`,
  } as CSSProperties;

  const inventoryNote = live.error
    ? `Rack inventory unavailable — ${live.error}`
    : !live.reachable
      ? "Head node unreachable — showing last known rack inventory"
      : topology.racks.length === 0
        ? live.loading
          ? "Loading live rack inventory…"
          : "No racks reported by /api/status"
        : `${topology.racks.length} racks · ${available.length}/${totalTrays} trays up (live${live.updatedAt ? ` · ${live.updatedAt}` : ""})`;

  const indicatorColor = live.error || !live.reachable ? "red" : topology.racks.length === 0 ? "yellow" : "green";

  return (
    <Box>
      <Box
        sx={{
          transition: "padding-right 0.2s ease",
          pr: scopeOpen ? `${SCOPE_PANEL_WIDTH + 24}px` : 0,
        }}
      >
        <Flex align="center" justify="between" wrap="wrap" gap="2" style={{ marginBottom: 16 }}>
          <Flex align="center" gap="3" wrap="wrap">
            <Text kind="body/bold/sm">{summary}</Text>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              {inventoryNote}
            </Text>
          </Flex>
          <Flex align="center" gap="3">
            {statusNote && (
              <Flex align="center" gap="2">
                <StatusIndicator color={indicatorColor} size="small" />
                <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                  {statusNote}
                </Text>
              </Flex>
            )}
            <Button
              kind="secondary"
              size="small"
              onClick={() => setScopeOpen((open) => !open)}
              aria-expanded={scopeOpen}
            >
              <SlidersHorizontal size={16} />
              Scope
            </Button>
          </Flex>
        </Flex>

        {children(selectedHosts, summary, selection)}
      </Box>

      <SidePanel
        bordered
        side="right"
        modal={false}
        closeOnClickOutside={false}
        open={scopeOpen}
        onOpenChange={setScopeOpen}
        slotHeading="Scope"
        style={panelStyle}
      >
        <ScopeTree topology={topology} selected={selected} onSelectedChange={setSelected} />
      </SidePanel>
    </Box>
  );
}
