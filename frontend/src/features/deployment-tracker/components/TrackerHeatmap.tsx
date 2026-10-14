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
import { useMemo, useState, type MouseEvent } from "react";
import { Button, Flex, SegmentedControl, Select, Tag, Text, TextInput } from "@nvidia/foundations-react-core";
import { Search } from "lucide-react";
import type { DeploymentTrackerReport, TrackerNode } from "../types";
import {
  NO_FILTER,
  STATE_COLOR,
  STATE_LABEL,
  STATE_ORDER,
  STATE_TAG,
  STATE_TEXT,
  countShown,
  formatCount,
  formatPercent,
  nodeMatches,
  plural,
  unitVisible,
  type TrackerFilter,
} from "../lib/trackerModel";

const TILE = 10;

/** One line of detail for the hovered tile, kept in one place rather than a tooltip per tile. */
function describe(node: TrackerNode): string {
  const parts = [
    node.node,
    `rack ${node.rack || "—"}`,
    STATE_LABEL[node.state],
    `Stage 1 ${node.stage1}`,
    `Stage 2 ${node.stage2}`,
  ];
  if (node.slurm) parts.push(`Slurm ${node.slurm}`);
  // A drain reason is already the Slurm state just printed; a failed stage is news.
  if (node.reason && !node.reason.startsWith("Slurm:")) parts.push(node.reason);
  return parts.join(" · ");
}

function Swatch({ color }: { color: string }) {
  return (
    <span
      aria-hidden
      style={{ display: "inline-block", width: TILE + 2, height: TILE + 2, borderRadius: 2, background: color, border: "1px solid var(--border-color-base)" }}
    />
  );
}

/**
 * Every node as a tile, one line per row of racks, racks separated by a gap.
 *
 * A tile is a plain element with a `title` rather than a tooltip component: an NC fleet is
 * ~4,600 tiles, and one hover handler on the container plus one detail line costs nothing,
 * where 4,600 tooltip instances would make the page crawl.
 */
export function TrackerHeatmap({ report }: { report: DeploymentTrackerReport }) {
  const [filter, setFilter] = useState<TrackerFilter>(NO_FILTER);
  const [hovered, setHovered] = useState<TrackerNode | null>(null);

  const byName = useMemo(() => {
    const map = new Map<string, TrackerNode>();
    for (const unit of report.units)
      for (const row of unit.rows) for (const rack of row.racks) for (const node of rack.nodes) map.set(node.node, node);
    return map;
  }, [report]);

  const shown = useMemo(() => countShown(report.units, filter), [report, filter]);
  const filtered = filter.state !== "all" || filter.unit !== "all" || filter.query.trim() !== "";

  const onHover = (e: MouseEvent<HTMLDivElement>) => {
    const name = (e.target as HTMLElement).dataset?.node;
    if (name) setHovered(byName.get(name) ?? null);
  };

  const stateItems = [
    { value: "all", children: "All" },
    ...STATE_ORDER.filter((s) => s !== "handed_over" || report.summary.counts.handed_over > 0).map((s) => ({
      value: s,
      children: STATE_LABEL[s],
    })),
  ];
  const unitItems = [
    { value: "all", children: "All scalable units" },
    ...report.units.map((u) => ({ value: u.id, children: u.label })),
  ];

  return (
    <Flex direction="col" gap="3">
      <Flex gap="3" wrap="wrap" align="center">
        <SegmentedControl
          size="small"
          aria-label="Filter by state"
          items={stateItems}
          value={filter.state}
          onValueChange={(v: string) => setFilter((f) => ({ ...f, state: v as TrackerFilter["state"] }))}
        />
        <div style={{ minWidth: 200 }}>
          <Select
            aria-label="Scalable unit"
            items={unitItems}
            value={filter.unit}
            onValueChange={(v: string) => setFilter((f) => ({ ...f, unit: v || "all" }))}
          />
        </div>
        <div style={{ minWidth: 240, flex: "1 1 240px" }}>
          <TextInput
            aria-label="Search nodes"
            placeholder="Search SU, row, rack, node, reason…"
            value={filter.query}
            onValueChange={(v: string) => setFilter((f) => ({ ...f, query: v }))}
            slotStart={<Search size={16} aria-hidden />}
          />
        </div>
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }} data-testid="heatmap-shown">
          {formatCount(shown)} nodes shown
        </Text>
        <Button kind="secondary" color="neutral" size="small" disabled={!filtered} onClick={() => setFilter(NO_FILTER)}>
          Clear filters
        </Button>
      </Flex>

      <Flex gap="4" wrap="wrap" align="center">
        {STATE_ORDER.filter((s) => s !== "handed_over" || report.summary.counts.handed_over > 0).map((s) => (
          <Flex key={s} gap="1" align="center" title={report.meta.definitions[s]}>
            <Swatch color={STATE_COLOR[s]} />
            <Text kind="body/regular/sm">{STATE_LABEL[s]}</Text>
          </Flex>
        ))}
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginLeft: "auto" }}>
          1 tile = 1 compute node · racks separated by a gap · hover for details
        </Text>
      </Flex>

      <Text
        kind="mono/sm"
        aria-live="polite"
        style={{ minHeight: 20, color: hovered ? "var(--text-color-primary)" : "var(--text-color-secondary)" }}
      >
        {hovered ? describe(hovered) : "Hover a tile to see the node."}
      </Text>

      <div onMouseOver={onHover} style={{ display: "flex", flexDirection: "column", gap: 20 }}>
        {report.units.filter((u) => unitVisible(filter, u)).map((unit) => (
          <section key={unit.id} aria-label={`${unit.label} heatmap`}>
            <Flex align="center" gap="2" wrap="wrap" style={{ marginBottom: 8 }}>
              <Text kind="title/sm">{unit.label}</Text>
              <Tag color={STATE_TAG[unit.status]} kind="outline" readOnly density="compact">
                {STATE_LABEL[unit.status]}
              </Tag>
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                {unit.rows.length === 1 ? unit.rows[0].label : plural(unit.rows.length, "row")} · {plural(unit.rack_count, "rack")} ·{" "}
                {plural(unit.total, "node")}
              </Text>
              <Text kind="body/bold/md" style={{ marginLeft: "auto", color: STATE_TEXT.completed }}>
                {formatPercent(unit.percent_complete)}
              </Text>
            </Flex>
            <div style={{ display: "grid", gridTemplateColumns: "minmax(64px, max-content) 1fr 48px", columnGap: 12, rowGap: 6, alignItems: "center" }}>
              {unit.rows.map((row) => (
                <div key={row.key} style={{ display: "contents" }}>
                  <Text kind="body/bold/sm">{row.label}</Text>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                    {row.racks.map((rack) => (
                      <div key={rack.rack} style={{ display: "flex", flexWrap: "wrap", gap: 2 }} title={`Rack ${rack.label}`}>
                        {rack.nodes.map((node) => {
                          const match = nodeMatches(filter, unit, row, rack, node);
                          return (
                            <span
                              key={node.node}
                              data-node={node.node}
                              data-state={node.state}
                              title={describe(node)}
                              style={{
                                width: TILE,
                                height: TILE,
                                borderRadius: 2,
                                background: STATE_COLOR[node.state],
                                border: node.state === "not_started" ? "1px solid var(--border-color-base)" : undefined,
                                boxSizing: "border-box",
                                opacity: match ? 1 : 0.15,
                              }}
                            />
                          );
                        })}
                      </div>
                    ))}
                  </div>
                  <Text kind="body/regular/sm" style={{ textAlign: "right" }}>
                    {formatPercent(row.percent_complete)}
                  </Text>
                </div>
              ))}
            </div>
          </section>
        ))}
      </div>
    </Flex>
  );
}
