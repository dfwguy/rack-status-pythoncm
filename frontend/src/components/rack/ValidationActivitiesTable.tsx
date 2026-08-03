import { useCallback, useEffect, useMemo, useState } from "react";
import Box from "@mui/material/Box";
import MuiButton from "@mui/material/Button";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import CircularProgress from "@mui/material/CircularProgress";
import { Card, Text, Tag, Flex } from "@nvidia/foundations-react-core";
import { LineChart, ScrollText } from "lucide-react";
import { TailLogDialog } from "../TailLogDialog";
import { ToolsLiveOutputDialog } from "../ToolsLiveOutputDialog";
import { JobLogContentDialog } from "../JobLogContentDialog";
import type { JobStdoutTailPayload } from "../../lib/rackLog";
import {
  dismissValidationActivity,
  fetchValidationActivities,
} from "../../services/validationActivitiesService";
import type { ValidationActivity } from "../../types/api";

type TagColor = "green" | "red" | "yellow" | "blue" | "gray" | "purple" | "teal";

const TERMINAL_PHASES = new Set(["done", "error", "timeout"]);

export interface ValidationActivitiesLogRow {
  log_file: string;
  display_rack: string;
  last_job: string;
}

export interface ValidationActivitiesTableProps {
  logDir: string;
  rack?: string;
  scope?: string;
  title?: string;
  pollMs?: number;
  /** Optional on-disk log rows for matching View Results (multirack). */
  logRows?: ValidationActivitiesLogRow[];
  onOpenParser?: (row: ValidationActivitiesLogRow, initialTab: number) => void;
}

function logBasename(path: string): string {
  const s = path.replace(/\\/g, "/").trim();
  const i = s.lastIndexOf("/");
  return i >= 0 ? s.slice(i + 1) : s;
}

function phaseColor(phase: string): TagColor {
  if (phase === "done") return "green";
  if (phase === "error" || phase === "timeout") return "red";
  if (phase === "running") return "yellow";
  return "blue";
}

function toolLabel(tool: string): string {
  switch (tool) {
    case "nccl-fullrack":
      return "NCCL Stage 1";
    case "llmb-nccl":
      return "NCCL (LLMB)";
    case "singlerack":
      return "NeMo singlerack";
    case "llmb-nemo-combined":
      return "NCCL + NeMo";
    case "nemo-xrack":
      return "NEMO x-rack";
    case "llama":
      return "Llama";
    case "mnubergemm":
      return "MNUBERGEMM";
    case "nvloom":
      return "nvloom";
    case "clusterkit":
      return "ClusterKit";
    case "external":
      return "External";
    default:
      return tool || "validation";
  }
}

function toolTagColor(tool: string): TagColor {
  if (tool.includes("nccl") || tool === "nccl-fullrack") return "purple";
  if (tool.includes("nemo") || tool === "singlerack") return "teal";
  return "blue";
}

function fmtWhen(iso: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function logRowForActivity(
  activity: ValidationActivity,
  logRows: ValidationActivitiesLogRow[] | undefined,
): ValidationActivitiesLogRow | null {
  if (!logRows?.length) return null;
  const keys = [activity.rack, ...activity.racks].map((r) => r.toLowerCase()).filter(Boolean);
  if (keys.length === 0) return null;
  let best: ValidationActivitiesLogRow | null = null;
  let bestScore = 0;
  for (const row of logRows) {
    const hay = `${row.display_rack} ${row.log_file}`.toLowerCase();
    let score = 0;
    for (const k of keys) if (hay.includes(k)) score++;
    if (score > bestScore) {
      bestScore = score;
      best = row;
    }
  }
  return bestScore > 0 ? best : null;
}

function viewLogsText(activity: ValidationActivity): string {
  const parts: string[] = [];
  if (activity.command_display) parts.push(`command: ${activity.command_display}`);
  if (activity.stdout_tail) parts.push(`stdout:\n${activity.stdout_tail}`);
  if (activity.stderr_tail) parts.push(`stderr:\n${activity.stderr_tail}`);
  return parts.join("\n\n") || "(no captured wrapper output yet)";
}

export function ValidationActivitiesTable({
  logDir,
  rack,
  scope,
  title = "Validation activities",
  pollMs = 6000,
  logRows,
  onOpenParser,
}: ValidationActivitiesTableProps) {
  const [activities, setActivities] = useState<ValidationActivity[]>([]);
  const [viewLogsId, setViewLogsId] = useState<string | null>(null);
  const [tailOpen, setTailOpen] = useState(false);
  const [tailTitle, setTailTitle] = useState("");
  const [tailPayload, setTailPayload] = useState<JobStdoutTailPayload | null>(null);
  const [staticLogPath, setStaticLogPath] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!logDir?.trim()) return;
    try {
      const rows = await fetchValidationActivities({
        logDir,
        rack: rack?.trim() || undefined,
        scope: scope?.trim() || undefined,
      });
      setActivities(rows);
    } catch {
      // keep last-known on transient errors
    }
  }, [logDir, rack, scope]);

  useEffect(() => {
    void load();
    if (!pollMs) return;
    const id = setInterval(() => void load(), pollMs);
    return () => clearInterval(id);
  }, [load, pollMs]);

  const anyRunning = useMemo(
    () => activities.some((a) => !TERMINAL_PHASES.has(a.phase)),
    [activities],
  );

  const viewActivity = useMemo(
    () => activities.find((a) => a.activity_id === viewLogsId || a.job_id === viewLogsId) ?? null,
    [activities, viewLogsId],
  );

  const openTail = (path: string, label: string) => {
    const p = path.trim();
    if (!p) return;
    setStaticLogPath(null);
    setTailTitle(`${label} — ${logBasename(p)}`);
    setTailPayload({ path: p, log_dir: logDir });
    setTailOpen(true);
  };

  const handleDismiss = async (activityId: string) => {
    if (await dismissValidationActivity(activityId)) {
      setActivities((prev) => prev.filter((a) => a.activity_id !== activityId));
    }
  };

  return (
    <Box sx={{ mb: 3 }}>
      <Flex gap="2" align="center" wrap="wrap" style={{ marginBottom: 12 }}>
        <Text kind="title/sm" style={{ marginRight: "auto" }}>
          {title}
        </Text>
        {anyRunning && <CircularProgress size={16} />}
        <Tag color={activities.length > 0 ? "blue" : "gray"} kind="outline" readOnly density="compact">
          {activities.length} tracked
        </Tag>
      </Flex>

      {activities.length === 0 ? (
        <Card style={{ padding: 32, textAlign: "center", marginBottom: 16 }}>
          <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
            No validation activities yet. Launches from Stage 1/2 or optional tests appear here with scheduler tasks and
            log links until dismissed.
          </Text>
        </Card>
      ) : (
        <TableContainer component={Paper} variant="outlined" sx={{ mb: 2 }}>
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                <TableCell>Rack(s)</TableCell>
                <TableCell>Stage / Tool</TableCell>
                <TableCell>Tasks</TableCell>
                <TableCell>Status</TableCell>
                <TableCell>Operator</TableCell>
                <TableCell>Launched</TableCell>
                <TableCell align="right">Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {activities.map((activity) => {
                const aid = activity.activity_id || activity.job_id;
                const logRow = logRowForActivity(activity, logRows);
                const hasParserLog = Boolean(logRow?.log_file);
                return (
                  <TableRow key={aid} hover>
                    <TableCell sx={{ fontWeight: 600 }}>
                      {activity.racks.length > 0 ? activity.racks.join(", ") : activity.rack || "—"}
                      <Text kind="mono/sm" style={{ display: "block", color: "var(--text-color-subtle)" }}>
                        {aid.slice(0, 8)}
                      </Text>
                    </TableCell>
                    <TableCell>
                      <Tag color={toolTagColor(activity.tool)} kind="outline" readOnly density="compact">
                        {toolLabel(activity.tool)}
                      </Tag>
                      {activity.stage && activity.stage !== "other" && (
                        <Tag color="gray" kind="outline" readOnly density="compact">
                          {activity.stage}
                        </Tag>
                      )}
                      {activity.dry_run && (
                        <Tag color="gray" kind="outline" readOnly density="compact">
                          dry run
                        </Tag>
                      )}
                    </TableCell>
                    <TableCell>
                      {(activity.tasks ?? []).length === 0 ? (
                        <Text kind="body/regular/sm" style={{ color: "var(--text-color-subtle)" }}>
                          —
                        </Text>
                      ) : (
                        (activity.tasks ?? []).map((t) => (
                          <Text
                            key={t.task_id || `${t.ordinal}-${t.label}`}
                            kind="mono/sm"
                            style={{ display: "block", marginBottom: 2 }}
                          >
                            {t.label}
                            {t.scheduler_ref ? ` ${t.scheduler_ref}` : ""}
                            {t.scheduler_state ? ` (${t.scheduler_state})` : ""}
                          </Text>
                        ))
                      )}
                    </TableCell>
                    <TableCell>
                      <Tooltip title={activity.error || ""} disableHoverListener={!activity.error} arrow>
                        <span style={{ display: "inline-flex" }}>
                          <Tag color={phaseColor(activity.phase)} kind="solid" readOnly density="compact">
                            {activity.phase}
                          </Tag>
                        </span>
                      </Tooltip>
                    </TableCell>
                    <TableCell>
                      <Text kind="body/regular/sm">{activity.operator || "—"}</Text>
                    </TableCell>
                    <TableCell>
                      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                        {fmtWhen(activity.created_at)}
                      </Text>
                    </TableCell>
                    <TableCell align="right">
                      <Box sx={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 0.5, flexWrap: "wrap" }}>
                        <MuiButton
                          size="small"
                          variant="outlined"
                          startIcon={<ScrollText size={16} />}
                          onClick={() => setViewLogsId(aid)}
                        >
                          Wrapper
                        </MuiButton>
                        {(activity.tasks ?? []).map((t) => (
                          <Box key={`logs-${t.task_id}`} sx={{ display: "inline-flex", gap: 0.5 }}>
                            {t.stdout_path && (
                              <MuiButton size="small" variant="text" onClick={() => openTail(t.stdout_path!, "StdOut")}>
                                StdOut
                              </MuiButton>
                            )}
                            {t.training_log_path && (
                              <MuiButton
                                size="small"
                                variant="text"
                                onClick={() => openTail(t.training_log_path!, "Training")}
                              >
                                Train
                              </MuiButton>
                            )}
                            {t.results_log_path && (
                              <MuiButton
                                size="small"
                                variant="text"
                                onClick={() => {
                                  setStaticLogPath(t.results_log_path!);
                                  setTailOpen(false);
                                }}
                              >
                                Results
                              </MuiButton>
                            )}
                          </Box>
                        ))}
                        {onOpenParser && (
                          <Tooltip title={hasParserLog ? "NCCL / NEMO charts" : "Results appear once log is on disk"}>
                            <span>
                              <MuiButton
                                size="small"
                                variant="contained"
                                disabled={!hasParserLog}
                                startIcon={<LineChart size={16} />}
                                onClick={() => logRow && onOpenParser(logRow, 1)}
                              >
                                Charts
                              </MuiButton>
                            </span>
                          </Tooltip>
                        )}
                        <MuiButton size="small" color="inherit" onClick={() => void handleDismiss(aid)}>
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

      <TailLogDialog
        open={tailOpen}
        onClose={() => setTailOpen(false)}
        title={tailTitle}
        payload={tailPayload}
        streamEndpoint="/api/tail-job-stdout-stream"
      />

      <JobLogContentDialog
        open={staticLogPath !== null}
        onClose={() => setStaticLogPath(null)}
        title={staticLogPath ? `Results — ${logBasename(staticLogPath)}` : "Results log"}
        logDir={logDir}
        path={staticLogPath}
      />

      <ToolsLiveOutputDialog
        open={viewActivity !== null}
        onClose={() => setViewLogsId(null)}
        title={
          viewActivity
            ? `${toolLabel(viewActivity.tool)} — ${viewActivity.racks.join(", ") || viewActivity.activity_id.slice(0, 8)}`
            : "Activity output"
        }
        logText={viewActivity ? viewLogsText(viewActivity) : ""}
        running={viewActivity ? !TERMINAL_PHASES.has(viewActivity.phase) : false}
      />
    </Box>
  );
}
