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
import { Card, Flex, Tag, Text } from "@nvidia/foundations-react-core";
import type { RackSummary, StageState, StageSummary } from "../types/api";
import { VALIDATION_STAGES, stageStateColor, stageStateLabel } from "../lib/validationStages";

export type ValidationSummaryCategory = "done" | "fail" | "ongoing" | "pending";

interface StatusSummaryProps {
  summary: RackSummary;
  updatedAt: string | null;
  legend?: string;
  /** Active validation filters (same strings as the rack table). When used with ``onToggleValidationCategory``, summary tags toggle filters. */
  validationFilters?: string[];
  onToggleValidationCategory?: (category: ValidationSummaryCategory) => void;
}

function validationTagColor(state: ValidationSummaryCategory): "green" | "red" | "yellow" | "blue" {
  switch (state) {
    case "done":
      return "green";
    case "fail":
      return "red";
    case "ongoing":
      return "yellow";
    case "pending":
      return "blue";
  }
}

const STAGE_ORDER: StageState[] = ["done", "ongoing", "fail", "pending"];

function StageLine({ label, tool, stage }: { label: string; tool: string; stage: StageSummary }) {
  return (
    <Flex gap="2" align="center" wrap="wrap">
      <Text kind="body/semibold/sm" style={{ color: "var(--text-color-secondary)", minWidth: 128 }}>
        {label} · {tool}
      </Text>
      {STAGE_ORDER.map((state) => (
        <Tag key={state} color={stageStateColor(state)} kind="outline" readOnly density="compact">
          {stage[state]} {stageStateLabel(state)}
        </Tag>
      ))}
    </Flex>
  );
}

export function StatusSummary({
  summary,
  updatedAt,
  legend,
  validationFilters = [],
  onToggleValidationCategory,
}: StatusSummaryProps) {
  const total = summary.done + summary.fail + summary.ongoing + summary.pending;
  const interactive = Boolean(onToggleValidationCategory);
  const hasStages = Boolean(summary.stage1 && summary.stage2);

  const chip = (category: ValidationSummaryCategory, count: number, label: string) => {
    const active = validationFilters.includes(category);
    const tag = (
      <Tag color={validationTagColor(category)} kind="solid" readOnly density="compact">
        {count} {label}
      </Tag>
    );
    if (!interactive) {
      return <span key={category}>{tag}</span>;
    }
    return (
      <Box
        key={category}
        component="button"
        type="button"
        onClick={() => onToggleValidationCategory?.(category)}
        aria-pressed={active}
        title={
          active
            ? `${label}: remove from filter (show more racks)`
            : `${label}: add to validation filter`
        }
        sx={{
          border: "none",
          background: "transparent",
          padding: 0,
          margin: 0,
          cursor: "pointer",
          borderRadius: 9999,
          display: "inline-flex",
          boxShadow: (theme) => (active ? `0 0 0 2px ${theme.palette.primary.main}` : "none"),
          "&:focus-visible": {
            outline: `2px solid`,
            outlineOffset: 2,
          },
        }}
      >
        {tag}
      </Box>
    );
  };

  return (
    <Card>
      <Flex gap="3" align="center" wrap="wrap">
        {chip("done", summary.done, "done")}
        {chip("fail", summary.fail, "fail")}
        {chip("ongoing", summary.ongoing, "ongoing")}
        {chip("pending", summary.pending, "pending")}
        <Text kind="body/semibold/md" style={{ color: "var(--text-color-secondary)" }}>
          {total} racks total
        </Text>
        {updatedAt && (
          <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)", marginLeft: "auto" }}>
            Updated: {new Date(updatedAt).toLocaleTimeString()}
          </Text>
        )}
      </Flex>
      {hasStages && (
        <Box sx={{ mt: 1.5, display: "flex", flexDirection: "column", gap: 0.75 }}>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-subtle)", display: "block" }}>
            By stage — a rack is validated once both stages pass:
          </Text>
          {VALIDATION_STAGES.map((stage) => {
            const stageSummary = summary[stage.key];
            if (!stageSummary) return null;
            return (
              <StageLine
                key={stage.key}
                label={stage.label}
                tool={stage.tool}
                stage={stageSummary}
              />
            );
          })}
        </Box>
      )}
      {legend && (
        <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)", marginTop: 8, display: "block" }}>
          {legend}
        </Text>
      )}
      {interactive && validationFilters.length > 0 && (
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginTop: 8, display: "block" }}>
          Filtering table by: {validationFilters.join(", ")} — click a highlighted tag again to remove it, or use Clear filters
          in the table.
        </Text>
      )}
    </Card>
  );
}
