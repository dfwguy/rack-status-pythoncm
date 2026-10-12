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
import { useMemo, useState } from "react";
import Box from "@mui/material/Box";
import Tooltip from "@mui/material/Tooltip";
import { Button, Text } from "@nvidia/foundations-react-core";
import type {
  NodeInventoryEntry,
  NodeSources,
  RackRow,
  RackHierarchy,
  StatusScalableUnit,
} from "../types/api";
import type { NvisNamingSchema } from "../types/naming";
import { inventoryByRack } from "../lib/multirackEligibility";
import {
  nodesInTrayOrder,
  ORCHESTRATOR_STATUS,
  SCHEDULER_STATUS,
  SCHEDULER_STATUS_ORDER,
  schedulerRackSummary,
  schedulerStatusOf,
  sourceName,
  type FloorStatusMode,
} from "../lib/nodeStatus";
import {
  barColumns,
  buildFloorLayout,
  buildScalableUnitLayout,
  floorLayoutFromHierarchy,
  HEALTH_COLOR,
  type FloorRack,
  type RackHealth,
} from "../lib/rackTopology";

interface RackFloorViewProps {
  rows: RackRow[];
  naming?: NvisNamingSchema;
  /** Backend-authoritative section/row/rack tree; preferred over client parsing. */
  hierarchy?: RackHierarchy;
  /**
   * This cluster's SU grouping, from the cluster record. Absent or empty means the cluster
   * has no SU model, and the toggle is not offered rather than showing one empty option.
   */
  scalableUnits?: StatusScalableUnit[];
  /**
   * The node inventory behind the racks. With it each tile draws one cell per node in tray order,
   * and the scheduler view becomes available; without it (older servers) tiles fall back to the
   * row's counts and only the orchestrator view exists.
   */
  nodes?: NodeInventoryEntry[] | null;
  /** Who answered for each view; an empty ``scheduler`` means no scheduler view to offer. */
  nodeSources?: NodeSources;
  onRackSelect?: (rack: string) => void;
}

const TRAY_UP = "var(--color-green-500, #2e7d32)";
const TRAY_DOWN = "var(--color-red-500, #c62828)";
const TRAY_UNK = "var(--color-gray-400, #bdbdbd)";

const HEALTH_LABEL: Record<RackHealth, string> = {
  healthy: "Healthy",
  partial: "Partial",
  down: "Down",
  unknown: "No data",
};

function validationColor(nemo: string): string | null {
  const v = (nemo || "").toLowerCase();
  if (v.includes("done") || v.includes("pass")) return "var(--color-green-500, #2e7d32)";
  if (v.includes("fail")) return "var(--color-red-500, #c62828)";
  if (v.includes("ongoing") || v.includes("run")) return "var(--color-blue-500, #1565c0)";
  if (v.includes("pending")) return "var(--color-yellow-500, #f9a825)";
  return null;
}

function TrayGrid({ rack }: { rack: FloorRack }) {
  const { trays, downCount, total } = rack.health;

  if (trays.length === 0) {
    // No per-tray detail — proportional count from sinfo (up vs down), wrapped into rows instead
    // of one long thin strip. Rows of 8 when the node count is a multiple of 8 (the common GPU
    // rack, e.g. 16 -> 2x8); otherwise ~2 balanced rows capped at 9 wide (NC's 18 -> 2x9). The
    // cells are a count, not per-tray state, so they fill up-then-down rather than by position.
    const cells = total > 0 ? total : 1;
    const downN = Math.min(downCount, cells);
    const cols = barColumns(cells);
    return (
      <Box sx={{ display: "grid", gridTemplateColumns: `repeat(${cols}, 1fr)`, gap: "2px", mt: 0.5 }}>
        {Array.from({ length: cells }).map((_, i) => (
          <Box
            key={i}
            sx={{
              height: 8,
              borderRadius: "1px",
              backgroundColor: i < cells - downN ? TRAY_UP : TRAY_DOWN,
            }}
          />
        ))}
      </Box>
    );
  }

  // Per-tray cells: up to 9 columns (matches typical 18-tray rack layout).
  const cols = Math.min(9, Math.max(1, Math.ceil(trays.length / 2)));
  return (
    <Box
      sx={{
        display: "grid",
        gridTemplateColumns: `repeat(${cols}, 1fr)`,
        gap: "2px",
        mt: 0.5,
      }}
    >
      {trays.map((t) => (
        <Box
          key={t.tray}
          title={`tray ${t.tray}: ${t.status}`}
          sx={{
            height: 9,
            borderRadius: "1px",
            backgroundColor: t.status === "up" ? TRAY_UP : t.status === "down" ? TRAY_DOWN : TRAY_UNK,
          }}
        />
      ))}
    </Box>
  );
}

/** One cell per node, in tray order, painted by whichever view is selected. */
function NodeGrid({ nodes, mode }: { nodes: NodeInventoryEntry[]; mode: FloorStatusMode }) {
  return (
    <Box sx={{ display: "grid", gridTemplateColumns: `repeat(${barColumns(nodes.length)}, 1fr)`, gap: "2px", mt: 0.5 }}>
      {nodes.map((node) => {
        const style =
          mode === "scheduler" ? SCHEDULER_STATUS[schedulerStatusOf(node)] : ORCHESTRATOR_STATUS[node.status];
        const raw = mode === "scheduler" && node.scheduler_state ? ` (${node.scheduler_state})` : "";
        return (
          <Box
            key={node.id}
            data-node-status={mode === "scheduler" ? schedulerStatusOf(node) : node.status}
            title={`${node.tray ? `tray ${node.tray} ` : ""}${node.id}: ${style.label}${raw}`}
            sx={{ height: 9, borderRadius: "1px", background: style.background }}
          />
        );
      })}
    </Box>
  );
}

/** "9 running · 9 drained": a rack's scheduler counts, in vocabulary order, empty states left out. */
function schedulerCountsText(nodes: NodeInventoryEntry[]): string {
  const { counts } = schedulerRackSummary(nodes);
  return SCHEDULER_STATUS_ORDER.filter((s) => counts[s] > 0)
    .map((s) => `${counts[s]} ${SCHEDULER_STATUS[s].label.toLowerCase()}`)
    .join(" · ");
}

interface RackTileProps {
  rack: FloorRack;
  /** This rack's nodes in tray order; empty when the server sent no inventory for it. */
  nodes: NodeInventoryEntry[];
  mode: FloorStatusMode;
  sources?: NodeSources;
  onRackSelect?: (rack: string) => void;
}

function RackTile({ rack, nodes, mode, sources, onRackSelect }: RackTileProps) {
  const { row, parts, health } = rack;
  const vColor = validationColor(row.nemo);
  const scheduler = mode === "scheduler" && nodes.length > 0 ? schedulerRackSummary(nodes) : null;
  // The tile's edge and dot: the rack verdict in the orchestrator view, the status most of the
  // rack is in for the scheduler.
  const edge = scheduler ? SCHEDULER_STATUS[scheduler.dominant].background : HEALTH_COLOR[health.health];
  const schedulerName = sourceName(sources, "scheduler");

  const tip = (
    <Box sx={{ fontSize: 12, lineHeight: 1.5 }}>
      <div><strong>{row.rack}</strong></div>
      <div>State: {HEALTH_LABEL[health.health]} ({health.upCount} up / {health.downCount} down of {health.total})</div>
      <div>sinfo: {row.sinfo || "-"}</div>
      {/* Both views, whichever is drawn: the disagreement between them is often the finding. */}
      {schedulerName && nodes.length > 0 && <div>{schedulerName}: {schedulerCountsText(nodes)}</div>}
      {row.nemo && row.nemo !== "-" && <div>Validation: {row.nemo}</div>}
      {row.last_job && row.last_job !== "-" && <div>Last job: {row.last_job}</div>}
    </Box>
  );

  return (
    <Tooltip title={tip} arrow placement="top">
      <Box
        role="button"
        tabIndex={0}
        onClick={() => onRackSelect?.(row.rack)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onRackSelect?.(row.rack);
          }
        }}
        sx={{
          position: "relative",
          width: 96,
          cursor: onRackSelect ? "pointer" : "default",
          border: "1px solid var(--border-color-base, rgba(0,0,0,0.18))",
          borderRadius: "4px",
          p: 0.75,
          // Room for the edge strip, drawn as a box rather than a border so a pattern (draining) fits.
          pl: "calc(6px + 4px)",
          backgroundColor: "var(--background-color-surface, transparent)",
          transition: "box-shadow 120ms, transform 120ms",
          "&:hover": { boxShadow: "0 2px 8px rgba(0,0,0,0.18)", transform: "translateY(-1px)" },
          "&:focus-visible": { outline: "2px solid var(--color-blue-500)", outlineOffset: 1 },
        }}
      >
        <Box
          aria-hidden
          sx={{ position: "absolute", left: 0, top: 0, bottom: 0, width: 4, borderRadius: "3px 0 0 3px", background: edge }}
        />
        <Box sx={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 0.5 }}>
          <Text kind="mono/sm" style={{ fontWeight: 700 }}>{parts.rackLabel}</Text>
          <Box sx={{ display: "flex", alignItems: "center", gap: "4px" }}>
            {vColor && (
              <Box sx={{ width: 7, height: 7, borderRadius: "50%", backgroundColor: vColor }} />
            )}
            <Box sx={{ width: 8, height: 8, borderRadius: "50%", background: edge }} />
          </Box>
        </Box>
        {nodes.length > 0 ? <NodeGrid nodes={nodes} mode={mode} /> : <TrayGrid rack={rack} />}
        <Text kind="body/regular/sm" style={{ display: "block", marginTop: 4, fontSize: 11, color: "var(--color-text-secondary, #6b6b6b)" }}>
          {scheduler
            ? `${scheduler.counts.idle}/${scheduler.total} idle`
            : `${health.upCount}/${health.total} up`}
        </Text>
      </Box>
    </Tooltip>
  );
}

function LegendSwatch({ color, label }: { color: string; label: string }) {
  return (
    <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
      <Box sx={{ width: 10, height: 10, borderRadius: "2px", background: color }} />
      <Text kind="body/regular/sm" style={{ fontSize: 12, color: "var(--color-text-secondary, #6b6b6b)" }}>{label}</Text>
    </Box>
  );
}

const GROUP_BY_STORAGE_KEY = "nvis.singlerack.floor.groupBy";
const STATUS_MODE_STORAGE_KEY = "nvis.singlerack.floor.statusMode";

function loadStatusMode(): FloorStatusMode {
  try {
    return localStorage.getItem(STATUS_MODE_STORAGE_KEY) === "scheduler" ? "scheduler" : "orchestrator";
  } catch {
    return "orchestrator";
  }
}

function loadGroupBy(): "row" | "su" {
  try {
    return localStorage.getItem(GROUP_BY_STORAGE_KEY) === "su" ? "su" : "row";
  } catch {
    return "row";
  }
}

/** Datacenter floor view: racks grouped by section (hall) -> row, each a tile with a tray micro-grid. */
export function RackFloorView({
  rows,
  naming,
  hierarchy,
  scalableUnits,
  nodes,
  nodeSources,
  onRackSelect,
}: RackFloorViewProps) {
  const sections = useMemo(
    () =>
      hierarchy && hierarchy.sections.length > 0
        ? floorLayoutFromHierarchy(rows, hierarchy)
        : buildFloorLayout(rows, naming),
    [rows, naming, hierarchy],
  );
  const suGroups = useMemo(
    () =>
      scalableUnits && scalableUnits.length > 0
        ? buildScalableUnitLayout(rows, scalableUnits, naming)
        : [],
    [rows, scalableUnits, naming],
  );
  // Rows stay the default: it is where the racks physically are, which is what somebody
  // walking the floor needs. SU is the fabric view, offered only where one is modelled.
  const [groupBy, setGroupBy] = useState<"row" | "su">(loadGroupBy);
  const changeGroupBy = (mode: "row" | "su") => {
    setGroupBy(mode);
    try {
      localStorage.setItem(GROUP_BY_STORAGE_KEY, mode);
    } catch {
      /* ignore persistence errors */
    }
  };
  const showSu = suGroups.length > 0 && groupBy === "su";
  const multiSection = sections.length > 1 || (sections[0]?.key ?? "") !== "";

  // The orchestrator view is the default and always there; the scheduler view only where a
  // scheduler answered for these nodes -- a floor painted entirely "unknown" would say nothing.
  const byRack = useMemo(() => inventoryByRack(nodes), [nodes]);
  const schedulerAvailable = Boolean(nodeSources?.scheduler) && Boolean(byRack && byRack.size > 0);
  const [statusMode, setStatusMode] = useState<FloorStatusMode>(loadStatusMode);
  const changeStatusMode = (mode: FloorStatusMode) => {
    setStatusMode(mode);
    try {
      localStorage.setItem(STATUS_MODE_STORAGE_KEY, mode);
    } catch {
      /* ignore persistence errors */
    }
  };
  const mode: FloorStatusMode = schedulerAvailable ? statusMode : "orchestrator";
  const anyInstalling = useMemo(() => (nodes ?? []).some((n) => n.status === "inst"), [nodes]);
  const tile = (r: FloorRack) => (
    <RackTile
      key={r.row.rack}
      rack={r}
      nodes={nodesInTrayOrder(byRack?.get(r.row.rack.trim().toLowerCase()) ?? [])}
      mode={mode}
      sources={nodeSources}
      onRackSelect={onRackSelect}
    />
  );

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <Box sx={{ display: "flex", flexWrap: "wrap", gap: 2, alignItems: "flex-start" }}>
        <Box
          data-testid="floor-legend"
          sx={{ display: "flex", flexWrap: "wrap", gap: 2, alignItems: "center", pt: 0.5 }}
        >
          {mode === "scheduler" ? (
            <>
              <Text kind="body/bold/sm">{sourceName(nodeSources, "scheduler")}</Text>
              {SCHEDULER_STATUS_ORDER.map((s) => (
                <LegendSwatch key={s} color={SCHEDULER_STATUS[s].background} label={SCHEDULER_STATUS[s].label} />
              ))}
            </>
          ) : (
            <>
              {schedulerAvailable && <Text kind="body/bold/sm">{sourceName(nodeSources, "orchestrator")}</Text>}
              <LegendSwatch color={HEALTH_COLOR.healthy} label="Healthy" />
              <LegendSwatch color={HEALTH_COLOR.partial} label="Partial (some trays down)" />
              <LegendSwatch color={HEALTH_COLOR.down} label="Down" />
              {anyInstalling && <LegendSwatch color={ORCHESTRATOR_STATUS.inst.background} label="Installing" />}
              <LegendSwatch color={HEALTH_COLOR.unknown} label="No data" />
            </>
          )}
        </Box>
        {(suGroups.length > 0 || schedulerAvailable) && (
          <Box sx={{ ml: "auto", display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 0.5 }}>
            {suGroups.length > 0 && (
              <Box role="group" aria-label="Group racks" sx={{ display: "flex", gap: 0.5 }}>
                {(["row", "su"] as const).map((g) => (
                  <Button
                    key={g}
                    kind={groupBy === g ? "primary" : "secondary"}
                    size="tiny"
                    onClick={() => changeGroupBy(g)}
                  >
                    {g === "row" ? "By row" : "By SU"}
                  </Button>
                ))}
              </Box>
            )}
            {schedulerAvailable && (
              <Box role="group" aria-label="Node status source" sx={{ display: "flex", gap: 0.5 }}>
                {(["orchestrator", "scheduler"] as const).map((m) => (
                  <Button
                    key={m}
                    kind={mode === m ? "primary" : "secondary"}
                    size="tiny"
                    aria-pressed={mode === m}
                    title={`Node status as ${sourceName(nodeSources, m)} reports it`}
                    onClick={() => changeStatusMode(m)}
                  >
                    {m === "orchestrator" ? "By orchestrator" : "By scheduler"}
                  </Button>
                ))}
              </Box>
            )}
          </Box>
        )}
      </Box>

      {showSu &&
        suGroups.map((unit) => (
          <Box key={unit.id} sx={{ display: "flex", alignItems: "flex-start", gap: 1.5 }}>
            <Box sx={{ minWidth: 96, pt: 1 }}>
              <Text kind="body/bold/sm" style={{ whiteSpace: "nowrap" }}>{unit.label}</Text>
              <Text
                kind="body/regular/sm"
                style={{ display: "block", fontSize: 11, color: "var(--color-text-secondary, #6b6b6b)" }}
              >
                {unit.rackCount} {unit.rackCount === 1 ? "rack" : "racks"}
              </Text>
            </Box>
            <Box sx={{ display: "flex", flexWrap: "wrap", gap: 1 }}>
              {unit.racks.map(tile)}
            </Box>
          </Box>
        ))}

      {!showSu && sections.map((section) => (
        <Box key={section.key || "all"} sx={{ display: "flex", flexDirection: "column", gap: 1 }}>
          {multiSection && (
            <Text kind="title/sm" style={{ display: "block" }}>
              {section.label}{" "}
              <Text kind="body/regular/sm" style={{ color: "var(--color-text-secondary, #6b6b6b)" }}>
                · {section.rackCount} racks
              </Text>
            </Text>
          )}
          {section.rows.map((floorRow) => (
            <Box key={floorRow.key} sx={{ display: "flex", alignItems: "flex-start", gap: 1.5 }}>
              <Box sx={{ minWidth: 64, pt: 1 }}>
                <Text kind="body/bold/sm" style={{ whiteSpace: "nowrap" }}>Row {floorRow.label}</Text>
              </Box>
              <Box sx={{ display: "flex", flexWrap: "wrap", gap: 1 }}>
                {floorRow.racks.map(tile)}
              </Box>
            </Box>
          ))}
        </Box>
      ))}
    </Box>
  );
}
