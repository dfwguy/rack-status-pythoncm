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
import Tooltip from "@mui/material/Tooltip";
import { Card, Flex, Tag, Text, Divider } from "@nvidia/foundations-react-core";
import type { RackRow } from "../../types/api";
import { validationStatusTooltip } from "../../lib/validationStatusTooltip";
import {
  VALIDATION_STAGES,
  stageStates,
  stageStateColor,
  stageStateLabel,
} from "../../lib/validationStages";

interface RackOverviewProps {
  row: RackRow;
  comment: string;
}

function ValidationTag({ row }: { row: RackRow }) {
  const tip = validationStatusTooltip(row);
  const inner = (() => {
    if (row.nemo === "done") return <Tag color="green" kind="solid" readOnly density="compact">done</Tag>;
    if (row.nemo === "fail") return <Tag color="red" kind="solid" readOnly density="compact">fail</Tag>;
    if (row.nemo.startsWith("ongoing"))
      return <Tag color="yellow" kind="solid" readOnly density="compact">{row.nemo}</Tag>;
    if (row.nemo === "pending") return <Tag color="blue" kind="solid" readOnly density="compact">pending</Tag>;
    return <Tag color="gray" kind="solid" readOnly density="compact">{row.nemo}</Tag>;
  })();
  if (!tip) return inner;
  return (
    <Tooltip title={<span style={{ display: "block", maxWidth: 420, whiteSpace: "pre-wrap" }}>{tip}</span>} enterDelay={350} arrow>
      <span style={{ display: "inline-flex", cursor: "default" }}>{inner}</span>
    </Tooltip>
  );
}

function StageBreakdown({ row }: { row: RackRow }) {
  const states = stageStates(row);
  return (
    <Flex gap="2" align="center" wrap="wrap">
      {VALIDATION_STAGES.map((stage) => {
        const state = states[stage.key];
        return (
          <Tag key={stage.key} color={stageStateColor(state)} kind="outline" readOnly density="compact">
            {stage.label} · {stage.tool}: {stageStateLabel(state)}
          </Tag>
        );
      })}
    </Flex>
  );
}

function InfoItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ minWidth: 120 }}>
      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block" }}>
        {label}
      </Text>
      <div style={{ marginTop: 4 }}>{children}</div>
    </div>
  );
}

export function RackOverview({ row, comment }: RackOverviewProps) {
  return (
    <Card style={{ padding: 24 }}>
      <Flex gap="4" align="center" wrap="wrap">
        <InfoItem label="Validation State">
          <ValidationTag row={row} />
        </InfoItem>

        <Divider orientation="vertical" />

        <InfoItem label="Stages (2 to validate)">
          <StageBreakdown row={row} />
        </InfoItem>

        <Divider orientation="vertical" />

        <InfoItem label="Last Job">
          <Text kind="body/regular/md" style={{ fontFamily: "monospace" }}>
            {row.last_job}
          </Text>
        </InfoItem>

        <Divider orientation="vertical" />

        <InfoItem label="Sinfo">
          <Text kind="body/regular/md">{row.sinfo}</Text>
        </InfoItem>

        <Divider orientation="vertical" />

        <InfoItem label="Device Status">
          <Text kind="body/regular/md">{row.ct_down_unk}</Text>
        </InfoItem>

        {comment && (
          <>
            <Divider orientation="vertical" />
            <InfoItem label="Comment">
              <Text kind="body/regular/md">{comment}</Text>
            </InfoItem>
          </>
        )}
      </Flex>
    </Card>
  );
}
