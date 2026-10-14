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
import { Card, Flex, Tag, Text } from "@nvidia/foundations-react-core";
import type { DeploymentState, TrackerRack, TrackerRollup, TrackerRow, TrackerUnit } from "../types";
import { STATE_ABBR, STATE_COLOR, STATE_LABEL, STATE_TAG, STATE_TEXT, formatCount, formatPercent, plural } from "../lib/trackerModel";
import { StateBar } from "./StateBar";

const ROW_COLUMNS: DeploymentState[] = ["completed", "in_progress", "remediation", "not_started"];

function StatusTag({ status }: { status: TrackerUnit["status"] }) {
  return (
    <Tag color={STATE_TAG[status]} kind="outline" readOnly density="compact">
      {STATE_LABEL[status]}
    </Tag>
  );
}

/** A card for one child of an SU: a row of racks, or a rack when the SU sits in one row. */
function GroupCard({
  label,
  group,
  footer,
  handedOver,
}: {
  label: string;
  group: TrackerRollup;
  footer: string;
  handedOver: boolean;
}) {
  const columns: DeploymentState[] = handedOver ? ["handed_over"] : ROW_COLUMNS;
  return (
    <Card>
      <Flex direction="col" gap="2">
        <Flex justify="between" align="baseline" gap="2">
          <Text kind="body/bold/md">{label}</Text>
          <Text kind="body/bold/md">{formatPercent(group.percent_complete)}</Text>
        </Flex>
        <div>
          <StatusTag status={group.status} />
        </div>
        <StateBar counts={group.counts} height={6} label={label} />
        <div style={{ display: "grid", gridTemplateColumns: `repeat(${columns.length}, 1fr)`, gap: 4, textAlign: "center" }}>
          {columns.map((state) => (
            <Flex key={state} direction="col" align="center">
              <Text kind="body/bold/md" style={{ color: STATE_TEXT[state] }}>
                {formatCount(group.counts[state])}
              </Text>
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }} title={STATE_LABEL[state]}>
                {STATE_ABBR[state]}
              </Text>
            </Flex>
          ))}
        </div>
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          {footer}
        </Text>
      </Flex>
    </Card>
  );
}

/**
 * The cards beside an SU's summary. Rows when the SU spans several; its racks when it sits in
 * one -- on NC every SU is seven racks of a single row, and one card per SU would repeat the
 * summary beside it instead of showing anything new.
 */
function UnitChildren({ unit }: { unit: TrackerUnit }) {
  const handedOver = unit.status === "handed_over";
  if (unit.rows.length > 1) {
    return unit.rows.map((row: TrackerRow) => (
      <GroupCard
        key={row.key}
        label={row.label}
        group={row}
        handedOver={handedOver}
        footer={`${plural(row.racks.length, "rack")} · ${plural(row.total, "node")}`}
      />
    ));
  }
  const row = unit.rows[0];
  return (row?.racks ?? []).map((rack: TrackerRack) => (
    <GroupCard
      key={rack.rack}
      label={rack.label}
      group={rack}
      handedOver={handedOver}
      footer={`Rack S1 ${rack.stage1} · S2 ${rack.stage2}`}
    />
  ));
}

function UnitSummary({ unit }: { unit: TrackerUnit }) {
  const finished = unit.counts.completed + unit.counts.handed_over;
  return (
    <Card style={{ borderLeft: `4px solid ${STATE_COLOR[unit.status === "not_started" ? "in_progress" : unit.status]}` }}>
      <Flex direction="col" gap="2">
        <Flex align="center" gap="2" wrap="wrap">
          <Text kind="title/md">{unit.label}</Text>
          <StatusTag status={unit.status} />
        </Flex>
        <Text kind="display/sm" style={{ color: STATE_TEXT.completed }}>
          {formatPercent(unit.percent_complete)}
        </Text>
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          {unit.handed_over_at ? `handed over ${unit.handed_over_at}` : "compute nodes complete"}
        </Text>
        <StateBar counts={unit.counts} label={unit.label} />
        <Text kind="body/regular/sm">
          {formatCount(finished)} / {formatCount(unit.total)} nodes · {plural(unit.rack_count, "rack")} ·{" "}
          {unit.rows.length === 1 ? unit.rows[0].label : plural(unit.rows.length, "row")}
        </Text>
      </Flex>
    </Card>
  );
}

/** One band per scalable unit: its summary, then a card per row of racks. */
export function TrackerUnits({ units }: { units: TrackerUnit[] }) {
  return (
    <Flex direction="col" gap="4">
      {units.map((unit) => (
        <div
          key={unit.id}
          style={{ display: "grid", gridTemplateColumns: "minmax(220px, 260px) 1fr", gap: 12, alignItems: "stretch" }}
        >
          <UnitSummary unit={unit} />
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))", gap: 10 }}>
            <UnitChildren unit={unit} />
          </div>
        </div>
      ))}
    </Flex>
  );
}
