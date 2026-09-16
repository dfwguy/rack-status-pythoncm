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
import { useMemo } from "react";
import Box from "@mui/material/Box";
import Tooltip from "@mui/material/Tooltip";
import { Text } from "@nvidia/foundations-react-core";
import type { RackRow, RackHierarchy } from "../types/api";
import type { NvisNamingSchema } from "../types/naming";
import {
  buildFloorLayout,
  floorRowHeading,
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
  const { trays, downCount, unknownCount, total } = rack.health;

  if (trays.length === 0) {
    // PythonCM can provide only aggregate node counts, without the legacy
    // ct_down_unk tray list. Keep the same two-row rack shape in that case so
    // an 18-node rack renders as 2 x 9 instead of one 18-cell strip.
    const cells = total > 0 ? total : 1;
    const downN = Math.min(downCount, cells);
    const unknownN = Math.min(unknownCount, cells - downN);
    const upN = cells - downN - unknownN;
    const cols = Math.min(9, Math.max(1, Math.ceil(cells / 2)));
    return (
      <Box
        aria-label={`${rack.row.rack} node status grid`}
        sx={{
          display: "grid",
          gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
          gap: "2px",
          mt: 0.5,
        }}
      >
        {Array.from({ length: cells }).map((_, i) => (
          <Box
            key={i}
            sx={{
              height: 9,
              borderRadius: "1px",
              backgroundColor: i < upN ? TRAY_UP : i < upN + downN ? TRAY_DOWN : TRAY_UNK,
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
      aria-label={`${rack.row.rack} node status grid`}
      sx={{
        display: "grid",
        gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
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

function RackTile({ rack, onRackSelect }: { rack: FloorRack; onRackSelect?: (rack: string) => void }) {
  const { row, parts, health } = rack;
  const color = HEALTH_COLOR[health.health];
  const vColor = validationColor(row.nemo);

  const tip = (
    <Box sx={{ fontSize: 12, lineHeight: 1.5 }}>
      <div><strong>{row.rack}</strong></div>
      <div>State: {HEALTH_LABEL[health.health]} ({health.upCount} up / {health.downCount} down of {health.total})</div>
      <div>node availability: {row.sinfo || "-"}</div>
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
          width: 96,
          cursor: onRackSelect ? "pointer" : "default",
          border: "1px solid var(--border-color-base, rgba(0,0,0,0.18))",
          borderLeft: `4px solid ${color}`,
          borderRadius: "4px",
          p: 0.75,
          backgroundColor: "var(--background-color-surface, transparent)",
          transition: "box-shadow 120ms, transform 120ms",
          "&:hover": { boxShadow: "0 2px 8px rgba(0,0,0,0.18)", transform: "translateY(-1px)" },
          "&:focus-visible": { outline: `2px solid ${color}`, outlineOffset: 1 },
        }}
      >
        <Box sx={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 0.5 }}>
          <Text kind="mono/sm" style={{ fontWeight: 700 }}>{parts.rackLabel}</Text>
          <Box sx={{ display: "flex", alignItems: "center", gap: "4px" }}>
            {vColor && (
              <Box sx={{ width: 7, height: 7, borderRadius: "50%", backgroundColor: vColor }} />
            )}
            <Box sx={{ width: 8, height: 8, borderRadius: "50%", backgroundColor: color }} />
          </Box>
        </Box>
        <TrayGrid rack={rack} />
        <Text kind="body/regular/sm" style={{ display: "block", marginTop: 4, fontSize: 11, color: "var(--color-text-secondary, #6b6b6b)" }}>
          {health.upCount}/{health.total} up
        </Text>
      </Box>
    </Tooltip>
  );
}

function LegendSwatch({ color, label }: { color: string; label: string }) {
  return (
    <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
      <Box sx={{ width: 10, height: 10, borderRadius: "2px", backgroundColor: color }} />
      <Text kind="body/regular/sm" style={{ fontSize: 12, color: "var(--color-text-secondary, #6b6b6b)" }}>{label}</Text>
    </Box>
  );
}

/** Datacenter floor view: racks grouped by section (hall) -> row, each a tile with a tray micro-grid. */
export function RackFloorView({ rows, naming, hierarchy, onRackSelect }: RackFloorViewProps) {
  const sections = useMemo(
    () =>
      hierarchy && hierarchy.sections.length > 0
        ? floorLayoutFromHierarchy(rows, hierarchy)
        : buildFloorLayout(rows, naming),
    [rows, naming, hierarchy],
  );
  const multiSection = sections.length > 1 || (sections[0]?.key ?? "") !== "";

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <Box sx={{ display: "flex", flexWrap: "wrap", gap: 2, alignItems: "center" }}>
        <LegendSwatch color={HEALTH_COLOR.healthy} label="Healthy" />
        <LegendSwatch color={HEALTH_COLOR.partial} label="Partial (some trays down)" />
        <LegendSwatch color={HEALTH_COLOR.down} label="Down" />
        <LegendSwatch color={HEALTH_COLOR.unknown} label="No data" />
      </Box>

      {sections.map((section) => (
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
                <Text kind="body/bold/sm" style={{ whiteSpace: "nowrap" }}>
                  {floorRowHeading(floorRow.label)}
                </Text>
              </Box>
              <Box sx={{ display: "flex", flexWrap: "wrap", gap: 1 }}>
                {floorRow.racks.map((r) => (
                  <RackTile key={r.row.rack} rack={r} onRackSelect={onRackSelect} />
                ))}
              </Box>
            </Box>
          ))}
        </Box>
      ))}
    </Box>
  );
}
