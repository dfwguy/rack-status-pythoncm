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
import { Card, Flex, Text } from "@nvidia/foundations-react-core";
import type { DeploymentState, DeploymentTrackerReport } from "../types";
import { STATE_COLOR, STATE_LABEL, STATE_TEXT, formatCount, formatPercent } from "../lib/trackerModel";

interface Tile {
  key: string;
  label: string;
  value: string;
  caption: string;
  /** Accent for the value and the card's leading edge; none for the neutral total. */
  state?: DeploymentState;
}

function share(n: number, total: number): string {
  return total ? formatPercent((100 * n) / total) : "0%";
}

/** The headline numbers: one tile per state, plus the total and the share complete. */
export function TrackerKpis({ report }: { report: DeploymentTrackerReport }) {
  const { summary } = report;
  const c = summary.counts;
  const t = summary.total;
  const tiles: Tile[] = [
    {
      key: "total",
      label: "Total nodes",
      value: formatCount(t),
      caption: `${summary.units} scalable unit${summary.units === 1 ? "" : "s"} · ${formatCount(summary.racks)} racks`,
    },
    { key: "not_started", label: STATE_LABEL.not_started, value: formatCount(c.not_started), caption: `${share(c.not_started, t)} of nodes`, state: "not_started" },
    { key: "in_progress", label: STATE_LABEL.in_progress, value: formatCount(c.in_progress), caption: `${share(c.in_progress, t)} · being worked`, state: "in_progress" },
    { key: "remediation", label: STATE_LABEL.remediation, value: formatCount(c.remediation), caption: "nodes need attention", state: "remediation" },
    { key: "completed", label: STATE_LABEL.completed, value: formatCount(c.completed), caption: `${share(c.completed, t)} of nodes`, state: "completed" },
  ];
  if (c.handed_over > 0) {
    tiles.push({ key: "handed_over", label: STATE_LABEL.handed_over, value: formatCount(c.handed_over), caption: "with the customer", state: "handed_over" });
  }
  tiles.push({
    key: "percent",
    label: "% complete",
    value: formatPercent(summary.percent_complete),
    caption: `${formatCount(c.completed + c.handed_over)} of ${formatCount(t)} nodes`,
    state: "completed",
  });

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(170px, 1fr))",
        gap: 12,
      }}
    >
      {tiles.map((tile) => (
        <Card
          key={tile.key}
          style={{ borderLeft: `4px solid ${tile.state && tile.state !== "not_started" ? STATE_COLOR[tile.state] : "var(--border-color-base)"}` }}
        >
          <Flex direction="col" gap="1">
            <Text kind="label/regular/sm" style={{ color: "var(--text-color-secondary)", textTransform: "uppercase", letterSpacing: "0.06em" }}>
              {tile.label}
            </Text>
            <Text
              kind="display/sm"
              data-testid={`kpi-${tile.key}`}
              style={{ color: tile.state && tile.state !== "not_started" ? STATE_TEXT[tile.state] : undefined }}
            >
              {tile.value}
            </Text>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              {tile.caption}
            </Text>
          </Flex>
        </Card>
      ))}
    </div>
  );
}
