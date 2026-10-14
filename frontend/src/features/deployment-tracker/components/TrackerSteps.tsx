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
import type { TrackerStep } from "../types";
import { STATE_COLOR, STATE_TEXT, formatCount, formatPercent } from "../lib/trackerModel";

/** A step's verdicts in the tracker's colours: done is completed, running is in progress. */
const STEP_PARTS = [
  { key: "done", label: "passed", color: STATE_COLOR.completed, text: STATE_TEXT.completed },
  { key: "ongoing", label: "running", color: STATE_COLOR.in_progress, text: STATE_TEXT.in_progress },
  { key: "fail", label: "failed", color: STATE_COLOR.remediation, text: STATE_TEXT.remediation },
  { key: "pending", label: "not run", color: STATE_COLOR.not_started, text: STATE_TEXT.not_started },
] as const;

function StepCard({ step }: { step: TrackerStep }) {
  const total = step.total || 1;
  return (
    <Card>
      <Flex direction="col" gap="2">
        <Flex justify="between" align="baseline" gap="2">
          <Text kind="body/bold/md">{step.label}</Text>
          <Text kind="title/md" style={{ color: STATE_TEXT.completed }}>
            {formatPercent(step.percent_complete)}
          </Text>
        </Flex>
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          {formatCount(step.total)} {step.unit}
        </Text>
        <div
          role="img"
          aria-label={`${step.label}: ${STEP_PARTS.map((p) => `${step[p.key]} ${p.label}`).join(", ")}`}
          style={{ display: "flex", height: 8, borderRadius: 4, overflow: "hidden", background: "var(--background-color-component-track)" }}
        >
          {STEP_PARTS.filter((p) => p.key !== "pending" && step[p.key] > 0).map((p) => (
            <div key={p.key} style={{ width: `${(100 * step[p.key]) / total}%`, background: p.color }} />
          ))}
        </div>
        <Flex gap="3" wrap="wrap">
          {STEP_PARTS.map((p) => (
            <Text
              key={p.key}
              kind="body/regular/sm"
              style={{ color: p.text }}
            >
              {formatCount(step[p.key])} {p.label}
            </Text>
          ))}
        </Flex>
      </Flex>
    </Card>
  );
}

/** The validation ladder: node stages first, then the full-rack stages. */
export function TrackerSteps({ steps }: { steps: TrackerStep[] }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 12 }}>
      {steps.map((step) => (
        <StepCard key={step.id} step={step} />
      ))}
    </div>
  );
}
