import { useState, useMemo, useEffect, useCallback, useRef } from "react";
import Box from "@mui/material/Box";
import MuiButton from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import FormControlLabel from "@mui/material/FormControlLabel";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Paper from "@mui/material/Paper";
import Snackbar from "@mui/material/Snackbar";
import Alert from "@mui/material/Alert";
import CircularProgress from "@mui/material/CircularProgress";
import { Card, Text, Tag, Button, Banner, Flex } from "@nvidia/foundations-react-core";
import { attachLaunchOptions, type LaunchOptions } from "../lib/rackCommandBodies";
import { LaunchOptionsDialog } from "../components/rack/LaunchOptionsDialog";
import { Layers, LineChart, Gauge, ScrollText } from "lucide-react";
import Tooltip from "@mui/material/Tooltip";
import { PageHeader } from "../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { ParserResultsDialog } from "../components/ParserResultsDialog";
import { TailLogDialog } from "../components/TailLogDialog";
import { ToolsLiveOutputDialog } from "../components/ToolsLiveOutputDialog";
import {
  buildMultirackTailBody,
  openRackLogInNewTab,
  validationOngoing,
  type TailLogStreamBody,
} from "../lib/rackLog";
import { validationStatusTooltip } from "../lib/validationStatusTooltip";
import type { MultirackJob, RackRow, RackStatusResponse } from "../types/api";

interface MultirackViewProps {
  data: RackStatusResponse | null;
  logDir: string;
}

function isRackEligible(row: RackRow): { eligible: boolean; reason: string } {
  if (row.nemo !== "done") return { eligible: false, reason: "need validation done" };
  const sinfoStates = row.sinfo.split(",").map((s) => s.trim().split("=")[0].toLowerCase()).filter(Boolean);
  if (sinfoStates.length === 0) return { eligible: false, reason: "no sinfo data" };
  if (sinfoStates.some((s) => s !== "idle")) return { eligible: false, reason: `sinfo ${row.sinfo}` };
  if (row.ct_down_unk.includes("DN:")) return { eligible: false, reason: "has DN nodes" };
  return { eligible: true, reason: "" };
}

type TagColor = "green" | "red" | "yellow" | "blue" | "gray";

function nemoColor(nemo: string): TagColor {
  if (nemo === "done") return "green";
  if (nemo === "fail") return "red";
  if (nemo.startsWith("ongoing")) return "yellow";
  if (nemo === "pending") return "blue";
  return "gray";
}

function MultirackValidationTag({ row }: { row: MultirackStatusRow }) {
  const tip = validationStatusTooltip({
    nemo: row.nemo,
    last_job: row.last_job,
    nemo_hint: row.nemo_hint,
  });
  const tag = (
    <Tag color={nemoColor(row.nemo)} kind="solid" readOnly density="compact">
      {row.nemo}
    </Tag>
  );
  if (!tip) return tag;
  return (
    <Tooltip title={<span style={{ display: "block", maxWidth: 420, whiteSpace: "pre-wrap" }}>{tip}</span>} enterDelay={350} arrow>
      <span style={{ display: "inline-flex", cursor: "default" }}>{tag}</span>
    </Tooltip>
  );
}

interface MultirackStatusRow {
  log_file: string;
  display_rack: string;
  nemo: string;
  last_job: string;
  sinfo: string;
  nemo_hint?: string;
}

/** log_file from API is usually a basename; strip any path for parser ?log_file= */
function logBasenameForParser(logFile: string): string {
  const s = logFile.replace(/\\/g, "/").trim();
  if (!s) return "";
  const i = s.lastIndexOf("/");
  return i >= 0 ? s.slice(i + 1) : s;
}

type MultirackLaunchKind = "nemo" | "llmb";

const LAUNCH_META: Record<MultirackLaunchKind, { label: string; asyncUrl: string }> = {
  nemo: { label: "Multirack NEMO validation", asyncUrl: "/api/run-nemo-xrack-async" },
  llmb: { label: "NCCL Microbenchmark (direct)", asyncUrl: "/api/run-llmb-nccl-async" },
};

const TERMINAL_PHASES = new Set(["done", "error", "timeout"]);

const LAUNCH_TITLE: Record<MultirackLaunchKind, string> = {
  nemo: "Launch Multirack NEMO Validation",
  llmb: "Launch NCCL Microbenchmark",
};

function jobKindLabel(kind: string): string {
  return kind === "llmb" ? "NCCL (direct)" : "NEMO x-rack";
}

function jobPhaseColor(phase: string): TagColor {
  if (phase === "done") return "green";
  if (phase === "error" || phase === "timeout") return "red";
  if (phase === "running") return "yellow";
  return "blue"; // starting / unknown
}

/** Best-effort match of a launched job to a log-derived results row (by rack ids). */
function logRowForJob(job: MultirackJob, mrRows: MultirackStatusRow[]): MultirackStatusRow | null {
  const jr = job.racks.map((r) => r.toLowerCase()).filter(Boolean);
  if (jr.length === 0) return null;
  let best: MultirackStatusRow | null = null;
  let bestScore = 0;
  for (const row of mrRows) {
    const hay = `${row.display_rack} ${row.log_file}`.toLowerCase();
    let score = 0;
    for (const r of jr) if (hay.includes(r)) score++;
    if (score > bestScore) {
      bestScore = score;
      best = row;
    }
  }
  return bestScore > 0 ? best : null;
}

function fmtWhen(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

export function MultirackView({ data, logDir }: MultirackViewProps) {
  const [dryRun, setDryRun] = useState(false);
  const [pendingKind, setPendingKind] = useState<MultirackLaunchKind | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [launchingKind, setLaunchingKind] = useState<MultirackLaunchKind | null>(null);
  const [snackbar, setSnackbar] = useState<{ open: boolean; message: string; severity: "success" | "error" | "info" }>({
    open: false, message: "", severity: "info",
  });

  // Launched jobs — durable state table shared across users/sessions (GET /api/multirack-jobs).
  const [jobs, setJobs] = useState<MultirackJob[]>([]);
  const [viewLogsJobId, setViewLogsJobId] = useState<string | null>(null);

  // Multirack status (on-disk logs / parser results)
  const [mrRows, setMrRows] = useState<MultirackStatusRow[]>([]);
  const [mrLoading, setMrLoading] = useState(false);
  const [mrError, setMrError] = useState("");

  const [parserOpen, setParserOpen] = useState(false);
  const [parserRack, setParserRack] = useState("");
  const [parserLogBasename, setParserLogBasename] = useState<string | undefined>();
  const [parserJobId, setParserJobId] = useState<string | undefined>();
  const [parserInitialTab, setParserInitialTab] = useState(1);

  const [tailStream, setTailStream] = useState<{ title: string; body: TailLogStreamBody } | null>(null);

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const prevTerminalRef = useRef(0);

  const openMultirackParser = useCallback((row: MultirackStatusRow, initialTab: number) => {
    const bn = logBasenameForParser(row.log_file);
    if (!bn) return;
    setParserRack(row.display_rack || "");
    setParserLogBasename(bn);
    setParserJobId(row.last_job && row.last_job !== "-" ? row.last_job : undefined);
    setParserInitialTab(initialTab);
    setParserOpen(true);
  }, []);

  const openMultirackLog = useCallback(
    async (row: MultirackStatusRow) => {
      const bn = logBasenameForParser(row.log_file);
      if (!bn) {
        setSnackbar({ open: true, message: "No log file for this row.", severity: "error" });
        return;
      }
      const r = await openRackLogInNewTab(logDir, { log_file: bn });
      if (!r.ok) {
        setSnackbar({ open: true, message: r.error, severity: "error" });
      }
    },
    [logDir]
  );

  const startMultirackTail = useCallback(
    (row: MultirackStatusRow) => {
      const bn = logBasenameForParser(row.log_file);
      if (!bn) {
        setSnackbar({ open: true, message: "No log file for this row.", severity: "error" });
        return;
      }
      setTailStream({
        title: `Tail log — ${row.display_rack || bn}`,
        body: buildMultirackTailBody(logDir, bn),
      });
    },
    [logDir]
  );

  // Fetch multirack status (defined before the poll effect so the poller can refresh results).
  const fetchMultirackStatus = useCallback(async () => {
    setMrLoading(true);
    setMrError("");
    try {
      const res = await fetch(`/api/multirack-status?log_dir=${encodeURIComponent(logDir)}`);
      const json = await res.json();
      if (json.ok && Array.isArray(json.rows)) {
        setMrRows(json.rows.map((r: Record<string, string>) => ({
          log_file: r.log_file || r.basename || "",
          display_rack: r.rack || r.display_rack || "",
          nemo: r.nemo || "-",
          last_job: r.last_job || "-",
          sinfo: r.sinfo || "-",
          nemo_hint: r.nemo_hint || "",
        })));
      } else {
        setMrRows([]);
        if (json.error) setMrError(json.error);
      }
    } catch (err) {
      setMrError(err instanceof Error ? err.message : "Failed to fetch");
      setMrRows([]);
    } finally {
      setMrLoading(false);
    }
  }, [logDir]);

  const rows = data?.rows || [];

  const eligibilityMap = useMemo(() => {
    const map = new Map<string, { eligible: boolean; reason: string }>();
    for (const row of rows) {
      map.set(row.rack, isRackEligible(row));
    }
    return map;
  }, [rows]);

  const eligibleCount = useMemo(() => {
    let count = 0;
    for (const v of eligibilityMap.values()) {
      if (v.eligible) count++;
    }
    return count;
  }, [eligibilityMap]);

  const handleToggle = (rack: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(rack)) next.delete(rack);
      else next.add(rack);
      return next;
    });
  };

  const handleSelectAllEligible = () => {
    const allEligible = rows.filter((r) => eligibilityMap.get(r.rack)?.eligible).map((r) => r.rack);
    if (selected.size === allEligible.length && allEligible.length > 0) {
      setSelected(new Set());
    } else {
      setSelected(new Set(allEligible));
    }
  };

  // Fetch the durable launched-jobs table (shared across users/sessions).
  const fetchJobs = useCallback(async () => {
    try {
      const res = await fetch(`/api/multirack-jobs?log_dir=${encodeURIComponent(logDir)}`);
      const json = await res.json();
      if (json.ok && Array.isArray(json.jobs)) {
        setJobs(json.jobs as MultirackJob[]);
      }
    } catch {
      // keep last-known jobs on transient errors
    }
  }, [logDir]);

  const handleLaunch = async (kind: MultirackLaunchKind, opts: LaunchOptions) => {
    if (selected.size === 0 || launchingKind) return;
    const meta = LAUNCH_META[kind];
    const racks = [...selected].sort();
    setLaunchingKind(kind);
    try {
      const body: Record<string, unknown> = { racks, log_dir: logDir };
      if (kind === "llmb") body.dry_run = dryRun;
      attachLaunchOptions(body, opts);
      const res = await fetch(meta.asyncUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const json = await res.json();
      if (json.ok) {
        setSnackbar({ open: true, message: `${meta.label} launched (${racks.length} racks)`, severity: "success" });
        setSelected(new Set());
        fetchJobs();
      } else {
        setSnackbar({ open: true, message: json.error || "Launch failed", severity: "error" });
      }
    } catch (err) {
      setSnackbar({ open: true, message: `Network error: ${err instanceof Error ? err.message : "unknown"}`, severity: "error" });
    } finally {
      setLaunchingKind(null);
    }
  };

  const confirmLaunch = (opts: LaunchOptions) => {
    const kind = pendingKind;
    setPendingKind(null);
    if (kind) void handleLaunch(kind, opts);
  };

  const handleDismissJob = useCallback(
    async (jobId: string) => {
      try {
        const res = await fetch(`/api/multirack-jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
        if (res.ok) {
          setViewLogsJobId((cur) => (cur === jobId ? null : cur));
          fetchJobs();
        } else {
          setSnackbar({ open: true, message: "Could not dismiss job.", severity: "error" });
        }
      } catch (err) {
        setSnackbar({ open: true, message: `Network error: ${err instanceof Error ? err.message : "unknown"}`, severity: "error" });
      }
    },
    [fetchJobs]
  );

  // Poll the jobs table; refresh on-disk results whenever a job newly reaches a terminal phase.
  useEffect(() => {
    fetchJobs();
    fetchMultirackStatus();
    pollRef.current = setInterval(fetchJobs, 6000);
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [fetchJobs, fetchMultirackStatus]);

  useEffect(() => {
    const terminal = jobs.filter((j) => TERMINAL_PHASES.has(j.phase)).length;
    if (terminal > prevTerminalRef.current) {
      fetchMultirackStatus();
    }
    prevTerminalRef.current = terminal;
  }, [jobs, fetchMultirackStatus]);

  const noRacks = !data || rows.length === 0;
  const anyJobRunning = jobs.some((j) => !TERMINAL_PHASES.has(j.phase));
  const viewLogsJob = jobs.find((j) => j.job_id === viewLogsJobId) || null;
  const viewLogsText = viewLogsJob
    ? [
        viewLogsJob.command_display ? `$ ${viewLogsJob.command_display}` : "",
        (viewLogsJob.stdout_tail || "").trim(),
        (viewLogsJob.stderr_tail || "").trim() ? `[stderr]\n${(viewLogsJob.stderr_tail || "").trim()}` : "",
        !viewLogsJob.stdout_tail && !viewLogsJob.stderr_tail && !TERMINAL_PHASES.has(viewLogsJob.phase)
          ? "(running — output is captured when the job finishes)"
          : "",
      ]
        .filter(Boolean)
        .join("\n\n")
    : "";

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}>
      <PageHeader
        contentId="multirack"
        title="Multirack Validation"
        description="Cross-rack NEMO validation runs spanning multiple racks. Select the available racks below, then launch a run."
      />

      {/* Inline rack selection + launch */}
      <Card style={{ marginBottom: 24, padding: 20 }}>
        <Flex gap="2" align="center" wrap="wrap" style={{ marginBottom: 12 }}>
          <Text kind="title/sm" style={{ marginRight: "auto" }}>
            Available racks
          </Text>
          <Tag color={eligibleCount > 0 ? "green" : "gray"} kind="outline" readOnly density="compact">
            {eligibleCount} eligible
          </Tag>
          {selected.size > 0 && (
            <Tag color="blue" kind="solid" readOnly density="compact">
              {selected.size} selected
            </Tag>
          )}
        </Flex>

        <Flex gap="2" align="center" wrap="wrap" style={{ marginBottom: 12 }}>
          <MuiButton size="small" variant="outlined" onClick={handleSelectAllEligible} disabled={eligibleCount === 0}>
            {selected.size === eligibleCount && eligibleCount > 0 ? "Clear selection" : `Select all eligible (${eligibleCount})`}
          </MuiButton>
          <FormControlLabel
            control={<Checkbox checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} size="small" />}
            label="Dry run (NCCL only — print command, don't submit)"
          />
          <Flex gap="2" align="center" wrap="wrap" style={{ marginLeft: "auto" }}>
            <Button
              kind="primary"
              size="small"
              onClick={() => setPendingKind("nemo")}
              disabled={noRacks || selected.size === 0 || launchingKind !== null}
            >
              <Layers size={16} style={{ marginRight: 6 }} />
              {launchingKind === "nemo" ? "Launching…" : `Launch Multirack Validation (${selected.size})`}
            </Button>
            <Button
              kind="secondary"
              size="small"
              onClick={() => setPendingKind("llmb")}
              disabled={noRacks || selected.size === 0 || launchingKind !== null}
            >
              <Gauge size={16} style={{ marginRight: 6 }} />
              {launchingKind === "llmb" ? "Launching…" : `Launch NCCL Microbenchmark (${selected.size})`}
            </Button>
          </Flex>
        </Flex>

        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 12, display: "block" }}>
          Eligible racks have validation done, Slurm sinfo showing only idle, and no DN trays. NCCL GPU scale = 72 × racks on
          GB300 NVL72.
        </Text>

        {noRacks ? (
          <Card style={{ padding: 32, textAlign: "center" }}>
            <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
              No rack status available yet. Rack data loads from the dashboard poll.
            </Text>
          </Card>
        ) : (
          <TableContainer component={Paper} variant="outlined" sx={{ maxHeight: "50vh" }}>
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  <TableCell padding="checkbox" />
                  <TableCell>Rack</TableCell>
                  <TableCell>Validation</TableCell>
                  <TableCell>Sinfo</TableCell>
                  <TableCell>Status</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {rows.map((row) => {
                  const { eligible, reason } = eligibilityMap.get(row.rack) || { eligible: false, reason: "unknown" };
                  return (
                    <TableRow
                      key={row.rack}
                      hover={eligible}
                      onClick={() => eligible && handleToggle(row.rack)}
                      sx={{
                        cursor: eligible ? "pointer" : "default",
                        opacity: eligible ? 1 : 0.5,
                      }}
                    >
                      <TableCell padding="checkbox">
                        <Checkbox
                          checked={selected.has(row.rack)}
                          disabled={!eligible}
                          onClick={(e) => e.stopPropagation()}
                          onChange={() => handleToggle(row.rack)}
                        />
                      </TableCell>
                      <TableCell sx={{ fontWeight: 600 }}>{row.rack}</TableCell>
                      <TableCell>
                        <MultirackValidationTag
                          row={{
                            log_file: "",
                            display_rack: row.rack,
                            nemo: row.nemo,
                            last_job: row.last_job,
                            sinfo: row.sinfo,
                            nemo_hint: row.nemo_hint,
                          }}
                        />
                      </TableCell>
                      <TableCell>
                        <Text kind="body/regular/sm">{row.sinfo}</Text>
                      </TableCell>
                      <TableCell>
                        {eligible ? (
                          <Tag color="green" kind="outline" readOnly density="compact">eligible</Tag>
                        ) : (
                          <Text kind="body/regular/sm" style={{ color: "var(--text-color-subtle)" }}>
                            {reason}
                          </Text>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </TableContainer>
        )}
      </Card>

      {/* Multi-rack jobs — durable state table (UUID per launch, shared across sessions) */}
      <Flex gap="2" align="center" wrap="wrap" style={{ marginBottom: 12 }}>
        <Text kind="title/sm" style={{ marginRight: "auto" }}>
          Multi-rack jobs
        </Text>
        {anyJobRunning && <CircularProgress size={16} />}
        <Tag color={jobs.length > 0 ? "blue" : "gray"} kind="outline" readOnly density="compact">
          {jobs.length} tracked
        </Tag>
      </Flex>

      {jobs.length === 0 ? (
        <Card style={{ padding: 32, textAlign: "center", marginBottom: 24 }}>
          <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
            No launched jobs yet. Launch a run above — each launch is tracked here with a job id and stays visible across
            sessions and other operators until dismissed.
          </Text>
        </Card>
      ) : (
        <TableContainer component={Paper} variant="outlined" sx={{ mb: 3 }}>
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                <TableCell>Racks</TableCell>
                <TableCell>Type</TableCell>
                <TableCell>Status</TableCell>
                <TableCell>Operator</TableCell>
                <TableCell>Launched</TableCell>
                <TableCell align="right">Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {jobs.map((job) => {
                const logRow = logRowForJob(job, mrRows);
                const hasLog = Boolean(logRow && logBasenameForParser(logRow.log_file));
                return (
                  <TableRow key={job.job_id} hover>
                    <TableCell sx={{ fontWeight: 600 }}>
                      {job.racks.length > 0 ? job.racks.join(", ") : "—"}
                      <Text kind="mono/sm" style={{ display: "block", color: "var(--text-color-subtle)" }}>
                        {job.job_id.slice(0, 8)}
                      </Text>
                    </TableCell>
                    <TableCell>
                      <Tag color={job.kind === "llmb" ? "purple" : "teal"} kind="outline" readOnly density="compact">
                        {jobKindLabel(job.kind)}
                      </Tag>
                      {job.dry_run && (
                        <Tag color="gray" kind="outline" readOnly density="compact">
                          dry run
                        </Tag>
                      )}
                    </TableCell>
                    <TableCell>
                      <Tooltip title={job.error || ""} disableHoverListener={!job.error} arrow>
                        <span style={{ display: "inline-flex" }}>
                          <Tag color={jobPhaseColor(job.phase)} kind="solid" readOnly density="compact">
                            {job.phase}
                          </Tag>
                        </span>
                      </Tooltip>
                    </TableCell>
                    <TableCell>
                      <Text kind="body/regular/sm">{job.operator || "—"}</Text>
                    </TableCell>
                    <TableCell>
                      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                        {fmtWhen(job.created_at)}
                      </Text>
                    </TableCell>
                    <TableCell align="right">
                      <Box sx={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 0.5, flexWrap: "wrap" }}>
                        <MuiButton
                          size="small"
                          variant="outlined"
                          startIcon={<ScrollText size={16} />}
                          onClick={() => setViewLogsJobId(job.job_id)}
                        >
                          View Logs
                        </MuiButton>
                        <Tooltip title={hasLog ? "NCCL / NEMO charts and Grafana" : "Results appear once the run's log is written"}>
                          <span>
                            <MuiButton
                              size="small"
                              variant="contained"
                              disabled={!hasLog}
                              startIcon={<LineChart size={16} />}
                              onClick={() => logRow && openMultirackParser(logRow, 1)}
                            >
                              View Results
                            </MuiButton>
                          </span>
                        </Tooltip>
                        <MuiButton size="small" color="inherit" onClick={() => void handleDismissJob(job.job_id)}>
                          Dismiss
                        </MuiButton>
                      </Box>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      {/* On-disk results & logs (parsed from the log directory) */}
      <Text kind="title/sm" style={{ display: "block", marginBottom: 12 }}>
        Results &amp; logs on disk
      </Text>

      {mrLoading && (
        <Box sx={{ display: "flex", justifyContent: "center", py: 6 }}>
          <CircularProgress />
        </Box>
      )}

      {mrError && (
        <div style={{ marginBottom: 16 }}>
          <Banner status="warning">
            {mrError}
          </Banner>
        </div>
      )}

      {!mrLoading && mrRows.length > 0 && (
        <TableContainer component={Paper} variant="outlined">
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                <TableCell>Racks</TableCell>
                <TableCell>Validation</TableCell>
                <TableCell>Last Job</TableCell>
                <TableCell>Sinfo</TableCell>
                <TableCell>Log File</TableCell>
                <TableCell align="right">Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {mrRows.map((row, i) => {
                const hasLog = Boolean(logBasenameForParser(row.log_file));
                const live = validationOngoing(row.nemo);
                return (
                  <TableRow key={i} hover>
                    <TableCell sx={{ fontWeight: 600 }}>{row.display_rack}</TableCell>
                    <TableCell>
                      <MultirackValidationTag row={row} />
                    </TableCell>
                    <TableCell>
                      <Text kind="body/regular/md" style={{ fontFamily: "monospace" }}>{row.last_job}</Text>
                    </TableCell>
                    <TableCell>
                      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>{row.sinfo}</Text>
                    </TableCell>
                    <TableCell>
                      <Text kind="mono/sm" style={{ color: "var(--text-color-secondary)" }}>
                        {row.log_file.length > 50 ? "..." + row.log_file.slice(-47) : row.log_file}
                      </Text>
                    </TableCell>
                    <TableCell align="right">
                      <Box sx={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 0.5, flexWrap: "wrap" }}>
                        {live ? (
                          <MuiButton
                            size="small"
                            variant="outlined"
                            disabled={!hasLog}
                            onClick={() => startMultirackTail(row)}
                          >
                            Tail Log
                          </MuiButton>
                        ) : (
                          <MuiButton
                            size="small"
                            variant="outlined"
                            disabled={!hasLog}
                            onClick={() => void openMultirackLog(row)}
                          >
                            Open Log
                          </MuiButton>
                        )}
                        <Tooltip title={hasLog ? "NCCL / NEMO charts and Grafana (parser.sh --json)" : "No log file"}>
                          <span>
                            <MuiButton
                              size="small"
                              variant="contained"
                              disabled={!hasLog}
                              startIcon={<LineChart size={16} />}
                              onClick={() => openMultirackParser(row, 1)}
                            >
                              View Results
                            </MuiButton>
                          </span>
                        </Tooltip>
                      </Box>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      {!mrLoading && mrRows.length === 0 && !mrError && (
        <Card style={{ padding: 48, textAlign: "center" }}>
          <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
            No multi-rack logs found on disk yet. Completed runs land here with links to their charts, Grafana, and raw logs.
          </Text>
        </Card>
      )}

      {/* Snackbar */}
      <Snackbar
        open={snackbar.open}
        autoHideDuration={5000}
        onClose={() => setSnackbar((s) => ({ ...s, open: false }))}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      >
        <Alert
          onClose={() => setSnackbar((s) => ({ ...s, open: false }))}
          severity={snackbar.severity}
          variant="filled"
          sx={{ width: "100%" }}
        >
          {snackbar.message}
        </Alert>
      </Snackbar>

      <TailLogDialog
        open={tailStream !== null}
        onClose={() => setTailStream(null)}
        title={tailStream?.title ?? "Tail log"}
        payload={tailStream?.body ?? null}
      />

      <ToolsLiveOutputDialog
        open={viewLogsJob !== null}
        onClose={() => setViewLogsJobId(null)}
        title={
          viewLogsJob
            ? `${jobKindLabel(viewLogsJob.kind)} — ${viewLogsJob.racks.join(", ") || viewLogsJob.job_id.slice(0, 8)}`
            : "Job output"
        }
        logText={viewLogsText}
        running={viewLogsJob ? !TERMINAL_PHASES.has(viewLogsJob.phase) : false}
      />

      <LaunchOptionsDialog
        open={pendingKind !== null}
        onClose={() => setPendingKind(null)}
        onConfirm={confirmLaunch}
        title={pendingKind ? LAUNCH_TITLE[pendingKind] : "Launch options"}
        subtitle={`${selected.size} rack(s) selected${pendingKind === "llmb" && dryRun ? " — dry run" : ""}`}
        confirmLabel="Launch"
        showNcclVersion={pendingKind === "llmb"}
        showIters={pendingKind === "llmb"}
      />

      <ParserResultsDialog
        open={parserOpen}
        onClose={() => {
          setParserOpen(false);
          setParserLogBasename(undefined);
          setParserJobId(undefined);
          setParserInitialTab(1);
        }}
        rack={parserRack}
        jobId={parserJobId}
        logDir={logDir}
        logFileBasename={parserLogBasename}
        initialTabIndex={parserInitialTab}
      />
    </Box>
  );
}
