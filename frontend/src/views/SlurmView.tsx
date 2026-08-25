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
import { Text } from "@nvidia/foundations-react-core";
import { PageHeader } from "../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { SqueuePanel } from "../components/SqueuePanel";
import { RecentJobsPanel } from "../components/RecentJobsPanel";
import { SinfoPanel } from "../components/SinfoPanel";

interface SlurmViewProps {
  logDir?: string;
}

export function SlurmView({ logDir }: SlurmViewProps) {
  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}>
      <PageHeader contentId="scheduler" title="Scheduler" description="Live jobs, recent history, and node availability from PythonCM direct execution." />

      <Text kind="body/regular/sm" style={{ marginBottom: 16, display: "block", color: "var(--text-color-secondary)" }}>
        Job log tail uses the log directory from settings (or the server default). All job state and actions on Nscale come from PythonCM ProgramRunner.
      </Text>

      <Box sx={{ display: "flex", flexDirection: "column", gap: 2, alignItems: "stretch" }}>
        <SqueuePanel logDir={logDir} active tableMaxHeight={560} />
        <RecentJobsPanel logDir={logDir} active limit={10} />
        <SinfoPanel active tableMaxHeight={560} />
      </Box>
    </Box>
  );
}
