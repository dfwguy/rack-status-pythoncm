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
import type { ReactNode } from "react";
import { Banner, Card, Flex, Text } from "@nvidia/foundations-react-core";
import type { DeploymentTrackerReport } from "../types";
import { STATE_LABEL, STATE_ORDER, formatUtc } from "../lib/trackerModel";
import { TrackerHeatmap } from "./TrackerHeatmap";
import { TrackerKpis } from "./TrackerKpis";
import { TrackerSteps } from "./TrackerSteps";
import { TrackerTable } from "./TrackerTable";
import { TrackerUnits } from "./TrackerUnits";

function Section({ title, caption, children }: { title: string; caption?: string; children: ReactNode }) {
  return (
    <Card>
      <Flex direction="col" gap="3">
        <Flex align="baseline" gap="2" wrap="wrap">
          <Text kind="title/sm">{title}</Text>
          {caption ? (
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              {caption}
            </Text>
          ) : null}
        </Flex>
        {children}
      </Flex>
    </Card>
  );
}

/**
 * The tracker body, shared by the in-app view and the published static page. Each shell draws its
 * own header; everything below it is this component, so the two cannot drift apart.
 */
export function DeploymentTracker({ report }: { report: DeploymentTrackerReport }) {
  const handed = report.summary.counts.handed_over > 0;
  return (
    <Flex direction="col" gap="4">
      {report.meta.stage2_loading ? (
        <Banner status="info">
          Stage 2 verdicts were still loading when this was generated, so some nodes may show as
          not started that have in fact run. Refresh in a minute.
        </Banner>
      ) : null}

      <TrackerKpis report={report} />

      <Section title="Validation steps" caption={handed ? "handed-over SUs are not counted here" : undefined}>
        <TrackerSteps steps={report.steps} />
      </Section>

      <Section title="Scalable units & rows" caption="compute nodes per row of racks">
        <TrackerUnits units={report.units} />
      </Section>

      <Section title="Node heatmap">
        <TrackerHeatmap report={report} />
      </Section>

      <Section title="Node detail" caption="open a scalable unit to see its rows, and a row to see its racks">
        <TrackerTable units={report.units} />
      </Section>

      <Section title="What the states mean">
        <dl style={{ display: "grid", gridTemplateColumns: "max-content 1fr", columnGap: 16, rowGap: 6, margin: 0 }}>
          {STATE_ORDER.filter((s) => s !== "handed_over" || handed).map((s) => (
            <div key={s} style={{ display: "contents" }}>
              <dt>
                <Text kind="body/bold/sm">{STATE_LABEL[s]}</Text>
              </dt>
              <dd style={{ margin: 0 }}>
                <Text kind="body/regular/sm">{report.meta.definitions[s]}</Text>
              </dd>
            </div>
          ))}
        </dl>
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          Stage 2 source: {report.meta.stage2_source || "—"} · cluster snapshot {formatUtc(report.meta.snapshot_at)}
        </Text>
        {report.unplaced?.length ? (
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }} data-testid="tracker-unplaced">
            Not counted — in the inventory but in no rack: {report.unplaced.join(", ")}
          </Text>
        ) : null}
      </Section>
    </Flex>
  );
}
