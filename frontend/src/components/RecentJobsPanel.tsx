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
import { useCallback, useEffect, useMemo, useState } from "react";
import Box from "@mui/material/Box";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import LinearProgress from "@mui/material/LinearProgress";
import { Text, Button } from "@nvidia/foundations-react-core";
import { fetchSlurmJson, type SlurmSacctJob } from "../lib/slurmHelpers";
import type { JobStdoutTailPayload } from "../lib/rackLog";
import { TailLogDialog } from "./TailLogDialog";
import { SortableHeaderCell } from "./SortableHeaderCell";
import { nextSortState, sortRows, type SortState } from "../lib/tableSort";

const NVIDIA_GREEN = "#76B900";

interface RecentJobsPanelProps {
  logDir?: string;
  /** When true, load whenever this toggles to true. */
  active: boolean;
  /** How many finished jobs to show (default 10). */
  limit?: number;
  tableMaxHeight?: string | number;
}

const COLS: { key: keyof SlurmSacctJob; label: string; mono?: boolean }[] = [
  { key: "job_id", label: "JobID", mono: true },
  { key: "name", label: "Name" },
  { key: "partition", label: "Partition" },
  { key: "nnodes", label: "Nodes" },
  { key: "nodelist", label: "NodeList", mono: true },
  { key: "elapsed", label: "Elapsed", mono: true },
  { key: "end", label: "End", mono: true },
  { key: "state", label: "State" },
];

/** Stable accessor: column index -> that job field, as text. */
const cellAt = (job: SlurmSacctJob, col: number) => String(job[COLS[col].key] ?? "");

/** Color a Slurm terminal state: green=success, red=hard failure, orange=timeout/preempt, gray=cancelled. */
function stateColor(state: string): string | undefined {
  const s = (state || "").split(" ")[0].toUpperCase();
  if (s === "COMPLETED") return "#4caf50";
  if (["FAILED", "NODE_FAIL", "OUT_OF_MEMORY", "BOOT_FAIL", "DEADLINE"].includes(s)) return "#f44336";
  if (["TIMEOUT", "PREEMPTED"].includes(s)) return "#ed6c02";
  if (s === "CANCELLED") return "var(--text-color-secondary)";
  return undefined;
}

export function RecentJobsPanel({ logDir, active, limit = 10, tableMaxHeight = 420 }: RecentJobsPanelProps) {
  const [loading, setLoading] = useState(false);
  const [cmd, setCmd] = useState("");
  const [bannerErr, setBannerErr] = useState("");
  const [jobs, setJobs] = useState<SlurmSacctJob[]>([]);
  const [sort, setSort] = useState<SortState>({ col: null, dir: "asc" });
  // Capped by `limit`, so this table is sorted but not paged. Default order is sacct's own
  // (most recently finished first), which stays reachable via a third header click.
  const sortedJobs = useMemo(() => sortRows(jobs, cellAt, sort), [jobs, sort]);

  const [tailOpen, setTailOpen] = useState(false);
  const [tailTitle, setTailTitle] = useState("");
  const [tailPayload, setTailPayload] = useState<JobStdoutTailPayload | null>(null);
  const [tailBusyJob, setTailBusyJob] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setBannerErr("");
    setJobs([]);
    setCmd("");
    try {
      const qs = new URLSearchParams({ limit: String(limit), state: "FINISHED" });
      const { res, data } = await fetchSlurmJson(`/api/scheduler-history?${qs}`);
      setCmd(data.command_display || "");
      if (!res.ok || data.ok === false) {
        setBannerErr(
          [data.error || `HTTP ${res.status}`, data.stderr ? `\n${String(data.stderr)}` : ""].filter(Boolean).join("")
        );
      }
      setJobs(Array.isArray(data.jobs) ? data.jobs : []);
    } catch (e) {
      setBannerErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [limit]);

  useEffect(() => {
    if (!active) return;
    void load();
  }, [active, load]);

  const openJobLog = useCallback(
    async (jobId: string) => {
      setTailBusyJob(jobId);
      try {
        const qs = new URLSearchParams({ job_id: jobId });
        const d = (logDir ?? "").trim();
        if (d) qs.set("log_dir", d);
        const { res, data } = await fetchSlurmJson(`/api/scheduler-job-log-path?${qs}`);
        if (!res.ok || data.ok === false || !data.path) {
          window.alert(data.error || `Could not resolve a log path for job ${jobId}.`);
          return;
        }
        const payload: JobStdoutTailPayload = d ? { path: data.path, log_dir: d } : { path: data.path };
        setTailTitle(`tail -f — ${data.path}`);
        setTailPayload(payload);
        setTailOpen(true);
      } catch (e) {
        window.alert(String(e));
      } finally {
        setTailBusyJob(null);
      }
    },
    [logDir]
  );

  return (
    <Paper variant="outlined" sx={{ borderLeft: `4px solid ${NVIDIA_GREEN}`, overflow: "hidden" }}>
      <Box sx={{ px: 2, py: 1.5, borderBottom: 1, borderColor: "divider", bgcolor: "action.hover", display: "flex", alignItems: "center", gap: 1, flexWrap: "wrap" }}>
        <Text kind="body/bold/md" style={{ fontWeight: 700, color: NVIDIA_GREEN }}>
          Recent finished jobs
        </Text>
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          last {limit} finished (sacct: completed + failed) — survives after squeue drops them
        </Text>
        <Button size="small" kind="secondary" onClick={() => void load()} disabled={loading || !active}>
          Refresh
        </Button>
        {loading && (
          <Box sx={{ flex: 1, minWidth: 120 }}>
            <LinearProgress />
          </Box>
        )}
      </Box>

      {cmd && (
        <Text kind="body/regular/sm" style={{ display: "block", paddingLeft: 16, paddingRight: 16, paddingTop: 8, fontFamily: "monospace", wordBreak: "break-all", color: "var(--text-color-secondary)" }}>
          {cmd}
        </Text>
      )}

      {bannerErr && (
        <Text kind="body/regular/md" style={{ display: "block", paddingLeft: 16, paddingRight: 16, paddingTop: 8, whiteSpace: "pre-wrap", color: "var(--text-color-error, #f44336)" }}>
          {bannerErr}
        </Text>
      )}

      {sortedJobs.length > 0 ? (
        <TableContainer sx={{ maxHeight: tableMaxHeight }}>
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                {COLS.map((c, ci) => (
                  <SortableHeaderCell
                    key={c.key}
                    label={c.label}
                    col={ci}
                    sortCol={sort.col}
                    sortDir={sort.dir}
                    onToggle={(col) => setSort((cur) => nextSortState(cur, col))}
                  />
                ))}
                <TableCell sx={{ fontWeight: 700, bgcolor: "action.hover", color: NVIDIA_GREEN }}>log</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {sortedJobs.map((j) => (
                <TableRow key={j.job_id} hover sx={{ "&:nth-of-type(even)": { bgcolor: "action.hover" } }}>
                  {COLS.map((c) => {
                    const value = j[c.key] ?? "";
                    const color = c.key === "state" ? stateColor(value) : undefined;
                    return (
                      <TableCell
                        key={c.key}
                        title={value}
                        sx={{
                          fontFamily: c.mono ? "monospace" : undefined,
                          fontSize: 12,
                          whiteSpace: "nowrap",
                          maxWidth: c.key === "nodelist" ? 320 : 220,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          color,
                          fontWeight: color && c.key === "state" ? 600 : undefined,
                        }}
                      >
                        {value}
                      </TableCell>
                    );
                  })}
                  <TableCell sx={{ whiteSpace: "nowrap" }}>
                    <Button
                      size="small"
                      kind="secondary"
                      onClick={() => void openJobLog(j.job_id)}
                      disabled={tailBusyJob === j.job_id}
                      title="Resolve the job's StdOut path and live-tail it"
                    >
                      open log
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      ) : (
        !loading && (
          <Text kind="body/regular/sm" style={{ display: "block", padding: 16, color: "var(--text-color-secondary)" }}>
            No finished jobs in the accounting window.
          </Text>
        )
      )}

      <TailLogDialog
        open={tailOpen}
        onClose={() => {
          setTailOpen(false);
          setTailPayload(null);
        }}
        title={tailTitle}
        payload={tailPayload}
        streamEndpoint="/api/tail-job-stdout-stream"
      />
    </Paper>
  );
}
