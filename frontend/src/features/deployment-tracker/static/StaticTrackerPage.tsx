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
import { Button, Flex, StatusMessage, Tag, Text } from "@nvidia/foundations-react-core";
import { Moon, Sun } from "lucide-react";
import { useThemeMode } from "../../../context/ThemeContext";
import { DeploymentTracker } from "../components/DeploymentTracker";
import { formatUtc, trackerSubtitle } from "../lib/trackerModel";
import type { DeploymentTrackerReport } from "../types";

/** The published page: a header saying what and when, then the shared tracker body. */
export function StaticTrackerPage({ report }: { report: DeploymentTrackerReport | null }) {
  const { mode, toggleTheme } = useThemeMode();
  return (
    <div style={{ minHeight: "100vh", background: "var(--background-color-surface-base)", color: "var(--text-color-primary)" }}>
      <div style={{ maxWidth: 1600, margin: "0 auto", padding: "24px 16px 48px" }}>
        <Flex justify="between" align="start" gap="3" wrap="wrap" style={{ marginBottom: 24 }}>
          <Flex direction="col" gap="1">
            <Flex align="center" gap="2" wrap="wrap">
              <Tag color="green" kind="solid" readOnly density="compact">NVIDIA</Tag>
              <Text kind="display/sm">
                {report?.meta.cluster ? `${report.meta.cluster} · Deployment Tracker` : "Deployment Tracker"}
              </Text>
            </Flex>
            {report ? (
              <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
                {trackerSubtitle(report)}
              </Text>
            ) : null}
          </Flex>
          <Flex align="center" gap="3">
            {report ? (
              <Flex direction="col" align="end">
                <Text kind="body/bold/sm">Snapshot {formatUtc(report.meta.generated_at)}</Text>
                <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                  Published from Rack Status · not a live feed
                </Text>
              </Flex>
            ) : null}
            <Button
              kind="secondary"
              color="neutral"
              size="small"
              aria-label={mode === "dark" ? "Switch to light theme" : "Switch to dark theme"}
              onClick={toggleTheme}
            >
              {mode === "dark" ? <Sun size={16} aria-hidden /> : <Moon size={16} aria-hidden />}
            </Button>
          </Flex>
        </Flex>
        {report ? (
          <DeploymentTracker report={report} />
        ) : (
          <StatusMessage
            slotHeading="No tracker data in this page"
            slotSubheading="This bundle was opened without a dataset. Export it from Rack Status (Analytics & Benchmarks › Deployment Tracker › Download static page)."
          />
        )}
      </div>
    </div>
  );
}
