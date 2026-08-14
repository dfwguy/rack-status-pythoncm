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
import Box from "@mui/material/Box";
import IconButton from "@mui/material/IconButton";
import TextField from "@mui/material/TextField";
import InputAdornment from "@mui/material/InputAdornment";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Checkbox, Flex, Text, Button } from "@nvidia/foundations-react-core";
import { Boxes, Server, Cpu, ChevronRight, ChevronDown, PowerOff, Search } from "lucide-react";
import { allAvailableHostIds, type ScopeCluster, type ScopeNode, type ScopeRack } from "./mockData";

type CheckState = boolean | "indeterminate";

/** Fixed row height (px). The virtualiser depends on every row being exactly this tall. */
const ROW_HEIGHT = 34;
/** Rows rendered above/below the viewport so fast scrolling doesn't flash blank. */
const OVERSCAN = 6;
/** Chrome above the scroller inside the side panel (heading + search + buttons + counts). */
const CHROME_HEIGHT = 360;
const MIN_VIEWPORT = 200;

/** Roll-up state for a parent given the selected set and its selectable children. */
function rollup(selected: Set<string>, childIds: string[]): CheckState {
  if (childIds.length === 0) return false;
  const picked = childIds.filter((id) => selected.has(id)).length;
  if (picked === 0) return false;
  if (picked === childIds.length) return true;
  return "indeterminate";
}

/** One flattened, absolutely-positioned row of the virtual list. */
type FlatRow =
  | { key: string; kind: "cluster" }
  | {
      key: string;
      kind: "rack";
      rack: ScopeRack;
      /** Selectable (up) host ids in this rack. */
      hostIds: string[];
      upCount: number;
      downCount: number;
      open: boolean;
    }
  | { key: string; kind: "node"; node: ScopeNode };

interface ScopeTreeProps {
  topology: ScopeCluster;
  selected: Set<string>;
  onSelectedChange: (next: Set<string>) => void;
}

/**
 * Reusable cluster → rack → tray selector driven by the LIVE rack inventory
 * (see ScopeExplorer: GET /api/status). Checkboxes at every level with
 * indeterminate roll-up; unavailable (down / unknown) trays are disabled and can
 * never be selected.
 *
 * Built for real scale — this cluster is 32 racks x 18 trays (576 hosts), so the
 * list is windowed (only the visible slice is in the DOM) and racks are collapsed
 * by default. A search box filters racks and trays; matching racks auto-expand.
 */
export function ScopeTree({ topology, selected, onSelectedChange }: ScopeTreeProps) {
  const [openRackIds, setOpenRackIds] = useState<Set<string>>(() => new Set());
  const [clusterOpen, setClusterOpen] = useState(true);
  const [query, setQuery] = useState("");
  const [scrollTop, setScrollTop] = useState(0);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Viewport height for the windowed list; recomputed on window resize.
  const [viewportH, setViewportH] = useState(() =>
    typeof window === "undefined" ? 480 : Math.max(MIN_VIEWPORT, window.innerHeight - CHROME_HEIGHT)
  );
  useEffect(() => {
    const onResize = () => setViewportH(Math.max(MIN_VIEWPORT, window.innerHeight - CHROME_HEIGHT));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const allHosts = useMemo(() => allAvailableHostIds(topology), [topology]);
  const totalNodes = useMemo(
    () => topology.racks.reduce((sum, r) => sum + r.nodes.length, 0),
    [topology]
  );
  const q = query.trim().toLowerCase();
  const searching = q !== "";

  /** Racks (and their visible trays) after applying the search filter. */
  const filteredRacks = useMemo(() => {
    return topology.racks
      .map((rack) => {
        const rackMatches = rack.label.toLowerCase().includes(q) || rack.id.toLowerCase().includes(q);
        const nodes =
          q === "" || rackMatches
            ? rack.nodes
            : rack.nodes.filter((n) => n.label.toLowerCase().includes(q) || n.id.toLowerCase().includes(q));
        return { rack, nodes, visible: q === "" || rackMatches || nodes.length > 0 };
      })
      .filter((r) => r.visible);
  }, [topology.racks, q]);

  /** Flatten the visible tree into one array so it can be windowed. */
  const rows = useMemo<FlatRow[]>(() => {
    const out: FlatRow[] = [{ key: "__cluster__", kind: "cluster" }];
    if (!clusterOpen) return out;
    for (const { rack, nodes } of filteredRacks) {
      const hostIds = rack.nodes.filter((n) => n.available).map((n) => n.id);
      // While searching, racks with hits open automatically; otherwise collapsed
      // by default (576 trays must never all mount at once).
      const open = searching || openRackIds.has(rack.id);
      out.push({
        key: `rack:${rack.id}`,
        kind: "rack",
        rack,
        hostIds,
        upCount: hostIds.length,
        downCount: rack.nodes.length - hostIds.length,
        open,
      });
      if (open) {
        for (const node of nodes) out.push({ key: `node:${node.id}`, kind: "node", node });
      }
    }
    return out;
  }, [filteredRacks, clusterOpen, openRackIds, searching]);

  // Filter changes invalidate scroll position.
  useEffect(() => {
    setScrollTop(0);
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [q]);

  const startIndex = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const endIndex = Math.min(rows.length, Math.ceil((scrollTop + viewportH) / ROW_HEIGHT) + OVERSCAN);
  const windowRows = rows.slice(startIndex, endIndex);

  const toggleRack = (id: string) =>
    setOpenRackIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const setMany = (ids: string[], on: boolean) => {
    const next = new Set(selected);
    ids.forEach((id) => (on ? next.add(id) : next.delete(id)));
    onSelectedChange(next);
  };

  const clusterState = rollup(selected, allHosts);
  const downTotal = totalNodes - allHosts.length;

  return (
    <Box>
      <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 8, color: "var(--text-color-secondary)" }}>
        Scope
      </Text>

      <TextField
        size="small"
        fullWidth
        placeholder="Search rack or tray…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        slotProps={{
          input: {
            startAdornment: (
              <InputAdornment position="start">
                <Search size={16} />
              </InputAdornment>
            ),
          },
        }}
        sx={{ mb: 1 }}
      />

      <Box sx={{ display: "flex", flexDirection: "column", gap: 1, mb: 1 }}>
        <Flex gap="2">
          <Button
            kind="primary"
            size="small"
            style={{ flex: 1 }}
            disabled={allHosts.length === 0}
            onClick={() => onSelectedChange(new Set(allHosts))}
          >
            Select all up
          </Button>
          <Button kind="primary" size="small" style={{ flex: 1 }} onClick={() => onSelectedChange(new Set())}>
            Clear
          </Button>
        </Flex>
        <Flex gap="2">
          <Button
            kind="primary"
            size="small"
            style={{ flex: 1 }}
            onClick={() => setOpenRackIds(new Set(topology.racks.map((r) => r.id)))}
          >
            Expand all
          </Button>
          <Button kind="primary" size="small" style={{ flex: 1 }} onClick={() => setOpenRackIds(new Set())}>
            Collapse all
          </Button>
        </Flex>
      </Box>

      <Text kind="body/regular/sm" style={{ display: "block", marginBottom: 4, color: "var(--text-color-secondary)" }}>
        {topology.racks.length} racks · {totalNodes} trays · {allHosts.length} up
        {downTotal > 0 ? ` · ${downTotal} down/unknown (not selectable)` : ""}
      </Text>
      <Text kind="body/regular/sm" style={{ display: "block", marginBottom: 8, color: "var(--text-color-secondary)" }}>
        {selected.size} selected
      </Text>

      {topology.racks.length === 0 ? (
        <Box sx={{ py: 1 }}>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            No rack inventory available.
          </Text>
        </Box>
      ) : (
        <div
          ref={scrollRef}
          onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
          style={{
            height: viewportH,
            overflowY: "auto",
            overflowX: "hidden",
            borderTop: "1px solid rgba(128, 128, 128, 0.25)",
            paddingTop: 4,
          }}
        >
          {searching && filteredRacks.length === 0 ? (
            <Box sx={{ pl: 3, py: 1 }}>
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                No racks or trays match “{query}”.
              </Text>
            </Box>
          ) : (
            /* Plain divs + inline styles on purpose: a per-row `sx` would mint a
               new emotion class for every scroll offset. */
            <div style={{ position: "relative", height: rows.length * ROW_HEIGHT }}>
              {windowRows.map((row, i) => {
                const index = startIndex + i;
                return (
                  <div
                    key={row.key}
                    style={{ position: "absolute", top: index * ROW_HEIGHT, left: 0, right: 0, height: ROW_HEIGHT }}
                  >
                    {row.kind === "cluster" && (
                      <ScopeRow
                        depth={0}
                        open={clusterOpen}
                        onToggleOpen={() => setClusterOpen((o) => !o)}
                        icon={<Boxes size={16} />}
                        label={topology.label}
                        checked={clusterState}
                        disabled={allHosts.length === 0}
                        onCheckedChange={(c) => setMany(allHosts, c === true)}
                      />
                    )}
                    {row.kind === "rack" && (
                      <ScopeRow
                        depth={1}
                        open={row.open}
                        onToggleOpen={() => toggleRack(row.rack.id)}
                        icon={<Server size={16} />}
                        label={row.rack.label}
                        hint={`${row.upCount} up${row.downCount > 0 ? ` · ${row.downCount} down` : ""}`}
                        checked={rollup(selected, row.hostIds)}
                        disabled={row.hostIds.length === 0}
                        onCheckedChange={(c) => setMany(row.hostIds, c === true)}
                      />
                    )}
                    {row.kind === "node" && (
                      <ScopeRow
                        depth={2}
                        icon={row.node.available ? <Cpu size={16} /> : <PowerOff size={16} />}
                        label={row.node.label}
                        hint={row.node.available ? undefined : row.node.status || "unavailable"}
                        disabled={!row.node.available}
                        checked={selected.has(row.node.id)}
                        onCheckedChange={(c) => setMany([row.node.id], c === true)}
                      />
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </Box>
  );
}

interface ScopeRowProps {
  depth: number;
  icon: ReactNode;
  label: string;
  checked: CheckState;
  onCheckedChange: (checked: CheckState) => void;
  disabled?: boolean;
  hint?: string;
  open?: boolean;
  onToggleOpen?: () => void;
}

function ScopeRow({ depth, icon, label, checked, onCheckedChange, disabled, hint, open, onToggleOpen }: ScopeRowProps) {
  const isBranch = typeof open === "boolean" && !!onToggleOpen;
  return (
    <Flex
      align="center"
      gap="1"
      style={{ paddingLeft: depth * 18, height: ROW_HEIGHT, overflow: "hidden" }}
    >
      {isBranch ? (
        <IconButton size="small" onClick={onToggleOpen} sx={{ p: 0.25, color: "text.secondary" }} aria-label={open ? "Collapse" : "Expand"}>
          {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </IconButton>
      ) : (
        <Box sx={{ width: 24, flexShrink: 0 }} />
      )}
      <Checkbox
        checked={checked}
        disabled={disabled}
        onCheckedChange={onCheckedChange}
        slotLabel={
          <Flex align="center" gap="2" style={{ opacity: disabled ? 0.5 : 1, whiteSpace: "nowrap" }}>
            <span style={{ display: "inline-flex", color: "var(--text-color-secondary)" }}>{icon}</span>
            <Text kind="body/regular/sm">{label}</Text>
            {hint && (
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                ({hint})
              </Text>
            )}
          </Flex>
        }
      />
    </Flex>
  );
}
