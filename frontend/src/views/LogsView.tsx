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
/**
 * Observability -> Logs.
 *
 * Two REAL sources. Nothing on this page is generated; an empty-but-true list is
 * preferred over a populated-but-fake one.
 *
 * 1. **On-disk validation / job logs** written by this tool on the head node under
 *    ``RACK_STATUS_LOG_DIR`` (``/home/cmods/benchmark/harish`` on NscaleTX). The
 *    inventory is assembled from ``GET /api/status`` (per-rack rows, opened by
 *    ``rack`` + ``job_id``) and ``GET /api/multirack-status`` (multirack and
 *    auxiliary log files, opened by basename). Every entry can be opened as plain
 *    text (``/api/rack-log-download``), previewed inline (``/api/rack-log-content``)
 *    or tailed live (``POST /api/tail-log-stream``) — the last one reuses the
 *    existing ``TailLogDialog``; the inline preview reuses ``CollapsibleLog``.
 *
 * 2. **Cluster log stream from Loki**, described by ``GET /api/observability/dashboards/logs``.
 *    When that route is missing, errors, or returns no panels, the section renders an
 *    explicit "not wired yet" state instead of inventing log lines.
 *
 * Coverage caveat that is shown to the operator, not hidden: Loki carries no per-node
 * or per-filename label on this cluster today, so the Grafana log panels cover
 * ObsVM-side exporter output only — real per-tray logs are the files in section 1.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import Box from "@mui/material/Box";
import LinearProgress from "@mui/material/LinearProgress";
import Link from "@mui/material/Link";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import { Banner, Button, Card, Flex, StatusMessage, Tag, Text } from "@nvidia/foundations-react-core";
import { ExternalLink, Eye, FileSearch, RefreshCw, ScrollText, Terminal } from "lucide-react";
import { PageHeader } from "../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { CollapsibleLog } from "../components/CollapsibleLog";
import { TailLogDialog } from "../components/TailLogDialog";
import { apiFetch } from "../lib/apiFetch";
import {
  buildMultirackTailBody,
  buildSingleRackTailBody,
  openRackLogInNewTab,
  type TailLogStreamBody,
} from "../lib/rackLog";
import { usePagedRows } from "../hooks/usePagedRows";
import { TablePager } from "../components/TablePager";

type TagColor = "green" | "blue" | "teal" | "yellow" | "red" | "gray";

/** ``file`` = resolved by basename (multirack / auxiliary); ``rack`` = resolved by rack + job id. */
type LogEntryKind = "file" | "rack";

interface LogEntry {
  key: string;
  kind: LogEntryKind;
  /** Basename for ``kind === "file"``; empty for rack-resolved entries. */
  file: string;
  rack: string;
  jobId: string;
  /** Validation state string straight from the backend row (``nemo`` column). */
  state: string;
  hint: string;
}

// ---------------------------------------------------------------------------
// Backend row shapes (both endpoints return loosely-typed string maps)
// ---------------------------------------------------------------------------

interface RawStatusRow {
  rack?: string;
  nemo?: string;
  last_job?: string;
  nemo_hint?: string;
}

interface RawStatusPayload {
  ok?: boolean;
  error?: string;
  log_dir?: string;
  rows?: RawStatusRow[];
}

interface RawMultirackRow {
  log_file?: string;
  basename?: string;
  rack?: string;
  display_rack?: string;
  nemo?: string;
  last_job?: string;
  nemo_hint?: string;
}

interface RawMultirackPayload {
  ok?: boolean;
  error?: string;
  log_dir?: string;
  rows?: RawMultirackRow[];
}

/** ``log_file`` from the API is usually already a basename; strip any path defensively. */
function basename(logFile: string): string {
  const s = String(logFile || "").replace(/\\/g, "/").trim();
  if (!s) return "";
  const i = s.lastIndexOf("/");
  return i >= 0 ? s.slice(i + 1) : s;
}

function cleanJobId(raw: string | undefined): string {
  const s = String(raw || "").trim();
  return !s || s === "-" ? "" : s;
}

function stateColor(state: string): TagColor {
  const s = state.trim().toLowerCase();
  if (s.startsWith("done") || s.startsWith("pass")) return "green";
  if (s.startsWith("fail") || s.startsWith("error")) return "red";
  if (s.startsWith("ongoing") || s.startsWith("running")) return "yellow";
  return "gray";
}

// ---------------------------------------------------------------------------
// Loki / Grafana contract (GET /api/observability/dashboards/logs)
// ---------------------------------------------------------------------------

interface ObsLogsPanelRaw {
  id?: string;
  uid?: string;
  title?: string;
  name?: string;
  url?: string;
  embed_url?: string;
  src?: string;
  open_url?: string;
  explore_url?: string;
  note?: string;
  live?: boolean;
}

interface ObsLogsPayload {
  ok?: boolean;
  error?: string;
  note?: string;
  /** Grafana only renders in an iframe when ``allow_embedding`` is on; default off. */
  embedding_enabled?: boolean;
  panels?: ObsLogsPanelRaw[];
  dashboards?: ObsLogsPanelRaw[];
}

interface ObsLogsPanel {
  key: string;
  title: string;
  embedUrl: string;
  openUrl: string;
  note: string;
  live: boolean;
}

/** Tolerant normalizer — accepts ``panels`` or ``dashboards`` and several url field names. */
function normalizeObsPanels(payload: ObsLogsPayload | null): ObsLogsPanel[] {
  const raw = payload?.panels ?? payload?.dashboards;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((p, i) => {
      const title = String(p.title || p.name || p.uid || `Panel ${i + 1}`).trim();
      return {
        key: String(p.id || p.uid || `${title}-${i}`),
        title,
        embedUrl: String(p.embed_url || p.src || p.url || "").trim(),
        openUrl: String(p.open_url || p.explore_url || p.url || "").trim(),
        note: String(p.note || "").trim(),
        live: p.live === true,
      };
    })
    .filter((p) => p.embedUrl || p.openUrl);
}

// ---------------------------------------------------------------------------

interface LogsViewProps {
  /**
   * Log directory to browse. Optional: when omitted the view uses whatever
   * ``GET /api/status`` reports (``RACK_STATUS_LOG_DIR`` on the head node). Pass the
   * App-level ``logDir`` here so a Settings override is honoured on this page too.
   */
  logDir?: string;
}

export function LogsView({ logDir: logDirProp }: LogsViewProps) {
  const [resolvedLogDir, setResolvedLogDir] = useState("");
  const logDir = (logDirProp || "").trim() || resolvedLogDir;

  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");

  const [tail, setTail] = useState<{ title: string; body: TailLogStreamBody } | null>(null);

  const [preview, setPreview] = useState<{ title: string; text: string; note: string } | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [actionError, setActionError] = useState("");

  const [obs, setObs] = useState<ObsLogsPayload | null>(null);
  const [obsError, setObsError] = useState("");
  const [obsLoading, setObsLoading] = useState(false);

  // -- on-disk log inventory -------------------------------------------------

  const loadEntries = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const want = (logDirProp || "").trim();
      const statusUrl = want ? `/api/status?log_dir=${encodeURIComponent(want)}` : "/api/status";
      const statusRes = await apiFetch(statusUrl, { cache: "no-store" });
      const status = (await statusRes.json()) as RawStatusPayload;
      if (!statusRes.ok || status.ok === false) {
        throw new Error(status.error || `GET /api/status failed (HTTP ${statusRes.status})`);
      }
      const dir = String(status.log_dir || "").trim() || want;
      setResolvedLogDir(dir);

      const mrUrl = dir
        ? `/api/multirack-status?log_dir=${encodeURIComponent(dir)}`
        : "/api/multirack-status";
      const mrRes = await apiFetch(mrUrl, { cache: "no-store" });
      const mr = (await mrRes.json()) as RawMultirackPayload;

      const out: LogEntry[] = [];

      // Multirack + auxiliary log files (resolved by basename).
      if (mrRes.ok && mr.ok !== false && Array.isArray(mr.rows)) {
        for (const r of mr.rows) {
          const file = basename(r.log_file || r.basename || "");
          if (!file) continue;
          out.push({
            key: `file:${file}`,
            kind: "file",
            file,
            rack: String(r.display_rack || r.rack || "").trim(),
            jobId: cleanJobId(r.last_job),
            state: String(r.nemo || "").trim(),
            hint: String(r.nemo_hint || "").trim(),
          });
        }
      }

      // Per-rack validation logs (resolved by rack + job id). Only rows that actually
      // reference a job are listed — a rack with no job has no log to open.
      if (Array.isArray(status.rows)) {
        for (const r of status.rows) {
          const rack = String(r.rack || "").trim();
          const jobId = cleanJobId(r.last_job);
          if (!rack || !jobId) continue;
          out.push({
            key: `rack:${rack}:${jobId}`,
            kind: "rack",
            file: "",
            rack,
            jobId,
            state: String(r.nemo || "").trim(),
            hint: String(r.nemo_hint || "").trim(),
          });
        }
      }

      out.sort((a, b) => (a.file || a.rack).localeCompare(b.file || b.rack));
      setEntries(out);
      if (mrRes.ok === false || mr.ok === false) {
        setError(mr.error || `GET /api/multirack-status failed (HTTP ${mrRes.status})`);
      }
    } catch (e) {
      setEntries([]);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [logDirProp]);

  // -- Loki panels -----------------------------------------------------------

  const loadObs = useCallback(async () => {
    setObsLoading(true);
    setObsError("");
    try {
      const res = await apiFetch("/api/observability/dashboards/logs", { cache: "no-store" });
      if (res.status === 404) {
        setObs(null);
        setObsError("not-wired");
        return;
      }
      const data = (await res.json()) as ObsLogsPayload;
      if (!res.ok || data.ok === false) {
        setObs(null);
        setObsError(data.error || `HTTP ${res.status}`);
        return;
      }
      setObs(data);
    } catch {
      setObs(null);
      setObsError("not-wired");
    } finally {
      setObsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadEntries();
    void loadObs();
  }, [loadEntries, loadObs]);

  // -- row actions -----------------------------------------------------------

  const openLog = useCallback(
    async (entry: LogEntry) => {
      const r = await openRackLogInNewTab(
        logDir,
        entry.kind === "file" ? { log_file: entry.file } : { rack: entry.rack, job_id: entry.jobId }
      );
      if (!r.ok) setActionError(r.error);
    },
    [logDir]
  );

  const tailLog = useCallback(
    (entry: LogEntry) => {
      const body =
        entry.kind === "file"
          ? buildMultirackTailBody(logDir, entry.file)
          : buildSingleRackTailBody(logDir, entry.rack, entry.jobId);
      setTail({ title: `Tail — ${entry.file || `${entry.rack} job ${entry.jobId}`}`, body });
    },
    [logDir]
  );

  const previewLog = useCallback(
    async (entry: LogEntry) => {
      const title = entry.file || `${entry.rack} · job ${entry.jobId}`;
      setPreviewBusy(true);
      setActionError("");
      setPreview(null);
      try {
        const qs = new URLSearchParams();
        if (logDir) qs.set("log_dir", logDir);
        if (entry.kind === "file") {
          qs.set("log_file", entry.file);
        } else {
          qs.set("rack", entry.rack);
          if (entry.jobId) qs.set("job_id", entry.jobId);
        }
        const res = await apiFetch(`/api/rack-log-content?${qs.toString()}`, { cache: "no-store" });
        const data = (await res.json()) as {
          ok?: boolean;
          error?: string;
          too_large?: boolean;
          size_bytes?: number;
          filename?: string;
          content?: string;
        };
        if (!res.ok || data.ok === false) {
          throw new Error(data.error || `HTTP ${res.status}`);
        }
        if (data.too_large) {
          setPreview({
            title,
            text: "",
            note: `${data.filename || title} is ${data.size_bytes ?? "?"} bytes — too large to inline. Use Open or Tail.`,
          });
          return;
        }
        setPreview({
          title: data.filename || title,
          text: String(data.content || ""),
          note: `${data.size_bytes ?? 0} bytes read from ${logDir || "the configured log directory"}`,
        });
      } catch (e) {
        setActionError(e instanceof Error ? e.message : String(e));
      } finally {
        setPreviewBusy(false);
      }
    },
    [logDir]
  );

  // -- derived ---------------------------------------------------------------

  const visible = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return entries;
    return entries.filter((e) =>
      `${e.file} ${e.rack} ${e.jobId} ${e.state}`.toLowerCase().includes(q)
    );
  }, [entries, filter]);

  // A validation log dir accumulates one file per rack per run, so this list only grows.
  const logsPage = usePagedRows(visible);

  const obsPanels = useMemo(() => normalizeObsPanels(obs), [obs]);
  const embeddingEnabled = obs?.embedding_enabled === true;

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}>
      <PageHeader
        contentId="obsLogs"
        title="Logs"
        description="Validation, job, and tail output read live from the head node log directory, plus the Loki-backed cluster log panels."
        actions={
          <Button
            kind="secondary"
            size="small"
            onClick={() => {
              void loadEntries();
              void loadObs();
            }}
            disabled={loading}
          >
            <RefreshCw size={16} />
            Refresh
          </Button>
        }
      />

      <Box sx={{ mb: 3 }}>
        <Banner status="info" kind="inline">
          Node-level log shipping is not enabled on this cluster: Loki carries no per-node or
          per-filename label, so the Grafana log panels below cover ObsVM-side exporter output
          only. Per-tray output lives in the validation log files, which are read live off the
          head node.
        </Banner>
      </Box>

      {error && (
        <Box sx={{ mb: 3 }}>
          <Banner status="error" kind="inline">
            {error}
          </Banner>
        </Box>
      )}

      {/* ---- 1. On-disk validation & job logs ------------------------------- */}
      <Card style={{ padding: 16, marginBottom: 24 }}>
        <Flex align="center" justify="between" wrap="wrap" gap="2" style={{ marginBottom: 12 }}>
          <Flex align="center" gap="2">
            <ScrollText size={18} />
            <Text kind="body/bold/md">Validation &amp; job logs on disk</Text>
          </Flex>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            {visible.length} of {entries.length} · {logDir || "log directory not resolved"}
          </Text>
        </Flex>

        <TextField
          size="small"
          fullWidth
          placeholder="Filter by file, rack, job id or state"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          sx={{ mb: 2, maxWidth: 420 }}
        />

        {loading && <LinearProgress sx={{ mb: 2 }} />}

        {visible.length === 0 ? (
          <StatusMessage
            size="small"
            slotMedia={<FileSearch />}
            slotHeading={entries.length === 0 ? "No logs on disk yet" : "No matching logs"}
            slotSubheading={
              entries.length === 0
                ? `Nothing readable in ${logDir || "the configured log directory"} yet. Completed validation and benchmark runs land here.`
                : "No log matches the current filter."
            }
          />
        ) : (
          <TableContainer component={Paper} variant="outlined" sx={{ maxHeight: 520 }}>
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  <TableCell>Log</TableCell>
                  <TableCell>Rack</TableCell>
                  <TableCell>Job</TableCell>
                  <TableCell>Validation</TableCell>
                  <TableCell align="right">Actions</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {logsPage.visible.map((e) => (
                  <TableRow key={e.key} hover>
                    <TableCell sx={{ fontFamily: "monospace", fontSize: 12, wordBreak: "break-all" }}>
                      {e.file || `newest log for ${e.rack}`}
                    </TableCell>
                    <TableCell>{e.rack || "—"}</TableCell>
                    <TableCell sx={{ fontFamily: "monospace", fontSize: 12 }}>{e.jobId || "—"}</TableCell>
                    <TableCell title={e.hint || undefined}>
                      {e.state ? (
                        <Tag color={stateColor(e.state)} kind="solid" readOnly density="compact">
                          {e.state}
                        </Tag>
                      ) : (
                        "—"
                      )}
                    </TableCell>
                    <TableCell align="right">
                      <Flex gap="1" justify="end" wrap="wrap">
                        <Button
                          kind="secondary"
                          color="neutral"
                          size="small"
                          onClick={() => void previewLog(e)}
                          disabled={previewBusy}
                          title="Read the file inline"
                        >
                          <Eye size={14} />
                          Preview
                        </Button>
                        <Button
                          kind="secondary"
                          color="neutral"
                          size="small"
                          onClick={() => tailLog(e)}
                          title="Stream the file live"
                        >
                          <Terminal size={14} />
                          Tail
                        </Button>
                        <Button
                          kind="secondary"
                          color="neutral"
                          size="small"
                          onClick={() => void openLog(e)}
                          title="Open the full file in a new tab"
                        >
                          <ExternalLink size={14} />
                          Open
                        </Button>
                      </Flex>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        )}
        <TablePager {...logsPage} noun="logs" />

        {previewBusy && <LinearProgress sx={{ mt: 2 }} />}

        {actionError && (
          <Box sx={{ mt: 2 }}>
            <Banner status="error" kind="inline">
              {actionError}
            </Banner>
          </Box>
        )}

        {preview && (
          <Box sx={{ mt: 2 }}>
            <Text kind="body/bold/sm" style={{ display: "block" }}>
              {preview.title}
            </Text>
            <Text
              kind="body/regular/sm"
              style={{ color: "var(--text-color-secondary)", display: "block", marginBottom: 6 }}
            >
              {preview.note}
            </Text>
            <CollapsibleLog text={preview.text} label="log" defaultOpen maxHeight={420} />
          </Box>
        )}
      </Card>

      {/* ---- 2. Loki via Grafana -------------------------------------------- */}
      <Card style={{ padding: 16 }}>
        <Flex align="center" justify="between" wrap="wrap" gap="2" style={{ marginBottom: 12 }}>
          <Flex align="center" gap="2">
            <FileSearch size={18} />
            <Text kind="body/bold/md">Cluster log stream (Loki via Grafana)</Text>
          </Flex>
          {obsPanels.length > 0 && (
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              {obsPanels.length} panel{obsPanels.length === 1 ? "" : "s"}
            </Text>
          )}
        </Flex>

        {obsLoading && <LinearProgress sx={{ mb: 2 }} />}

        {obs?.note && (
          <Box sx={{ mb: 2 }}>
            <Banner status="info" kind="inline">
              {obs.note}
            </Banner>
          </Box>
        )}

        {obsPanels.length === 0 ? (
          <StatusMessage
            size="small"
            slotMedia={<FileSearch />}
            slotHeading="Cluster log stream not wired yet"
            slotSubheading={
              obsError && obsError !== "not-wired"
                ? `GET /api/observability/dashboards/logs returned: ${obsError}. No log lines are shown rather than synthetic ones.`
                : "GET /api/observability/dashboards/logs is not serving any Loki panels yet. No log lines are shown rather than synthetic ones."
            }
          />
        ) : (
          <>
            {!embeddingEnabled && (
              <Box sx={{ mb: 2 }}>
                <Banner status="warning" kind="inline">
                  Grafana iframe embedding is not enabled (it answers with
                  {" "}
                  <code>X-Frame-Options: deny</code>), so these panels open in a new tab instead of
                  rendering inline.
                </Banner>
              </Box>
            )}
            <Flex direction="col" gap="3">
              {obsPanels.map((p) => (
                <Box key={p.key}>
                  <Flex align="center" justify="between" wrap="wrap" gap="2" style={{ marginBottom: 6 }}>
                    <Flex align="center" gap="2">
                      <Text kind="body/bold/sm">{p.title}</Text>
                      {p.live && (
                        <Tag color="green" kind="solid" readOnly density="compact">
                          LIVE
                        </Tag>
                      )}
                    </Flex>
                    {p.openUrl && (
                      <Link
                        href={p.openUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        sx={{ display: "inline-flex", alignItems: "center", gap: 0.5, fontSize: 13 }}
                      >
                        Open in Grafana
                        <ExternalLink size={13} />
                      </Link>
                    )}
                  </Flex>
                  {p.note && (
                    <Text
                      kind="body/regular/sm"
                      style={{ color: "var(--text-color-secondary)", display: "block", marginBottom: 6 }}
                    >
                      {p.note}
                    </Text>
                  )}
                  {embeddingEnabled && p.embedUrl && (
                    <iframe
                      src={p.embedUrl}
                      title={p.title}
                      style={{ width: "100%", height: 320, border: 0, borderRadius: 4 }}
                    />
                  )}
                </Box>
              ))}
            </Flex>
          </>
        )}
      </Card>

      <TailLogDialog
        open={Boolean(tail)}
        onClose={() => setTail(null)}
        title={tail?.title || "Tail log"}
        payload={tail?.body ?? null}
      />
    </Box>
  );
}
