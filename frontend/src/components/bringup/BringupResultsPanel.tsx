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
import { useMemo, useState } from "react";
import Box from "@mui/material/Box";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import LinearProgress from "@mui/material/LinearProgress";
import { Card, Flex, Text, Tag, Button, Banner } from "@nvidia/foundations-react-core";
import { RefreshCw, ScrollText, LineChart } from "lucide-react";
import { useBringupNode } from "../../hooks/useBringupNode";
import { RefreshIntervalSelect } from "../RefreshIntervalSelect";
import { loadRefreshMs, saveRefreshMs } from "../../lib/refreshInterval";
import type { BringupJob } from "../../types/bringup";
import type { JobStdoutTailPayload } from "../../lib/rackLog";
import { TailLogDialog } from "../TailLogDialog";
import { JobLogContentDialog } from "../JobLogContentDialog";
import { ParserResultsDialog } from "../ParserResultsDialog";

/** Persisted auto-refresh cadence for this panel. */
const REFRESH_KEY = "nvis.bringup.refreshMs";

/** "just now" / "3m ago" — the cache means shown data can be minutes old. */
function agoLabel(ts: number | null): string {
  if (!ts) return "";
  const secs = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (secs < 45) return "just now";
  const mins = Math.round(secs / 60);
  return mins < 60 ? `${mins}m ago` : `${Math.round(mins / 60)}h ago`;
}

/** NCCL jobs whose logs the busbw chart parser understands. */
const NCCL_CHART_JOBS = new Set([
  "allreduce",
  "alltoall",
  "allreduce-nonvlink",
  "alltoall-nonvlink",
  "allreduce-forcesharp",
]);

type Tone = "green" | "red" | "yellow" | "blue" | "gray";

function statusTone(status: string): Tone {
  switch (status.toUpperCase()) {
    case "DONE":
      return "green";
    case "FAIL":
      return "red";
    case "RUNNING":
      return "blue";
    case "RETRY":
    case "INCOMPLETE":
      return "yellow";
    default:
      return "gray";
  }
}

/** Remote file for a job column: results/<node>/<job>.txt (run suffix normalized). */
function jobLogPath(home: string, node: string, jobName: string): string {
  const base = home.replace(/\/+$/, "");
  const file = `${jobName.replace(/_run/, "-run")}.txt`;
  return `${base}/results/${node}/${file}`;
}

interface BringupResultsPanelProps {
  node: string;
  logDir?: string;
}

/**
 * Read-only per-node bring-up results from Travis's automation, shown above the
 * node action tabs. Status/reason come from status-check.sh and the metric from
 * daily-report.sh (see backend/bringup.py). Each job row can open its raw log or,
 * for NCCL jobs, the busbw charts.
 */
export function BringupResultsPanel({ node, logDir }: BringupResultsPanelProps) {
  const [refreshMs, setRefreshMs] = useState(() => loadRefreshMs(REFRESH_KEY));
  const { data, loading, error, lastUpdated, refresh } = useBringupNode(node, refreshMs);
  const [logJob, setLogJob] = useState<BringupJob | null>(null);
  const [chartJob, setChartJob] = useState<BringupJob | null>(null);

  const changeRefresh = (ms: number) => {
    setRefreshMs(ms);
    saveRefreshMs(REFRESH_KEY, ms);
  };

  const home = data?.home ?? "";

  // A RUNNING job is actively written (sbatch --output), so follow it live; every
  // other status is a finished file, shown whole. This is why "Open log" branches.
  const logIsLive = (logJob?.status ?? "").toUpperCase() === "RUNNING";
  const logFilePath = logJob && home ? jobLogPath(home, node, logJob.name) : null;

  // Stable payload identity: TailLogDialog restarts its `tail -f` stream whenever
  // this object's reference changes, and the panel re-renders on every parent poll
  // (rack-locks ~5s, rack-status ~15s). An inline literal here made the log flicker
  // and eventually read "(stopped)". Memoize so it only changes with the real inputs.
  const logPayload = useMemo<JobStdoutTailPayload | null>(
    () => (logFilePath && logIsLive ? { path: logFilePath, log_dir: logDir } : null),
    [logFilePath, logIsLive, logDir],
  );
  const chartPath = chartJob && home ? jobLogPath(home, node, chartJob.name) : undefined;

  return (
    <Card style={{ marginBottom: 24 }}>
      <Flex justify="between" align="center" wrap="wrap" style={{ marginBottom: 8 }}>
        <div>
          <Text kind="body/bold/md" style={{ display: "block" }}>
            Bring-up results
          </Text>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            Live from the cluster automation (results + reports){data?.slurm_state ? ` \u00b7 Slurm ${data.slurm_state}` : ""}
            {lastUpdated ? ` \u00b7 updated ${agoLabel(lastUpdated)}` : ""}
          </Text>
        </div>
        <Flex gap="2" align="center">
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            Auto-refresh
          </Text>
          <RefreshIntervalSelect
            valueMs={refreshMs}
            onChangeMs={changeRefresh}
            size="small"
            aria-label="Auto-refresh interval"
          />
          <Button kind="secondary" size="small" onClick={refresh} disabled={loading}>
            <RefreshCw size={16} style={{ marginRight: 4 }} />
            Refresh
          </Button>
        </Flex>
      </Flex>

      {/* Only a thin bar while refreshing: cached rows stay on screen underneath,
          which is the point of the cache — a background tick must not blank the table. */}
      {loading && (
        <Box aria-label="Loading bring-up results" sx={{ my: 1 }}>
          <LinearProgress />
        </Box>
      )}

      {error && (
        <Banner status="warning">
          {error.includes("disabled") || error.includes("unavailable")
            ? "Bring-up automation is not available for this deployment."
            : `Could not load bring-up results: ${error}`}
        </Banner>
      )}

      {!loading && !error && data && data.jobs.length === 0 && (
        <Banner status="info">No bring-up results recorded for {node} yet.</Banner>
      )}

      {data && data.jobs.length > 0 && (
        <TableContainer>
          <Table size="small" aria-label={`Bring-up results for ${node}`}>
            <TableHead>
              <TableRow>
                <TableCell>Job</TableCell>
                <TableCell>Status</TableCell>
                <TableCell>Metric</TableCell>
                <TableCell>Reason</TableCell>
                <TableCell align="right">Log</TableCell>
                <TableCell align="right">Charts</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {data.jobs.map((job) => {
                const notRun = job.status.toUpperCase() === "NOT_RUN" || !job.status;
                const canChart = NCCL_CHART_JOBS.has(job.base) && !notRun;
                return (
                  <TableRow key={job.name} hover>
                    <TableCell>
                      <Text kind="mono/sm">{job.name}</Text>
                    </TableCell>
                    <TableCell>
                      <Tag color={statusTone(job.status)} kind="solid" readOnly density="compact">
                        {job.status || "UNKNOWN"}
                      </Tag>
                    </TableCell>
                    <TableCell>
                      {job.metric ? (
                        <Text kind="body/regular/sm">
                          {job.metric}
                          {job.metric_label ? ` ${job.metric_label}` : ""}
                        </Text>
                      ) : (
                        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                          &mdash;
                        </Text>
                      )}
                    </TableCell>
                    <TableCell>
                      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                        {job.reason || ""}
                      </Text>
                    </TableCell>
                    <TableCell align="right">
                      <Button
                        kind="secondary"
                        size="tiny"
                        onClick={() => setLogJob(job)}
                        disabled={notRun}
                        aria-label={`Open ${job.name} log`}
                      >
                        <ScrollText size={15} />
                      </Button>
                    </TableCell>
                    <TableCell align="right">
                      <Button
                        kind="secondary"
                        size="tiny"
                        onClick={() => setChartJob(job)}
                        disabled={!canChart}
                        aria-label={`Open ${job.name} charts`}
                      >
                        <LineChart size={15} />
                      </Button>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      <TailLogDialog
        open={logJob !== null && logIsLive}
        onClose={() => setLogJob(null)}
        title={logJob ? `tail -f \u2014 ${node} / ${logJob.name}` : ""}
        streamEndpoint="/api/tail-job-stdout-stream"
        payload={logPayload}
      />

      <JobLogContentDialog
        open={logJob !== null && !logIsLive}
        onClose={() => setLogJob(null)}
        title={logJob ? `${node} / ${logJob.name}` : ""}
        path={!logIsLive ? logFilePath : null}
        logDir={logDir}
      />

      <ParserResultsDialog
        open={chartJob !== null}
        onClose={() => setChartJob(null)}
        rack=""
        logDir={logDir}
        logPath={chartPath}
      />
    </Card>
  );
}
