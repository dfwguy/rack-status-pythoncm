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
import { useCallback, useEffect, useState } from "react";
import Box from "@mui/material/Box";
import Link from "@mui/material/Link";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import LinearProgress from "@mui/material/LinearProgress";
import { Text, Button, Modal, ModalCloseButton, Flex } from "@nvidia/foundations-react-core";
import { XCircle } from "lucide-react";
import {
  fetchSlurmJson,
  findSqueueJobColumnIndex,
  formatSlurmCmdDetail,
  parseSlurmStdOutPathFromScontrolText,
  schedulerJobIdForAction,
  type SlurmCmdJson,
} from "../lib/slurmHelpers";
import { parseSqueueStdout } from "../lib/parseSqueueStdout";
import { slurmReasonTone, slurmToneColor } from "../lib/slurmReasonTone";
import type { JobStdoutTailPayload } from "../lib/rackLog";
import { TailLogDialog } from "./TailLogDialog";
import { useSortedPagedRows } from "../hooks/useSortedPagedRows";
import { TablePager } from "./TablePager";
import { SortableHeaderCell } from "./SortableHeaderCell";
import { apiFetch } from "../lib/apiFetch";

const NVIDIA_GREEN = "#76B900";

/** Stable accessor for the sort hook: squeue rows are a plain string matrix. */
const cellAt = (row: string[], col: number) => row[col] ?? "";

interface SqueuePanelProps {
  logDir?: string;
  /** When true, load squeue whenever this toggles to true (e.g. toolbar dialog open). */
  active: boolean;
  /** Max height for table + raw pre (dialog uses smaller). */
  tableMaxHeight?: string | number;
}

export function SqueuePanel({ logDir, active, tableMaxHeight = 480 }: SqueuePanelProps) {
  const [loading, setLoading] = useState(false);
  const [cmd, setCmd] = useState("");
  const [bannerErr, setBannerErr] = useState("");
  const [stderrNote, setStderrNote] = useState("");
  const [stdout, setStdout] = useState("");
  const [parsed, setParsed] = useState<ReturnType<typeof parseSqueueStdout> | null>(null);

  const [scontrolJobId, setScontrolJobId] = useState<string | null>(null);
  const [scontrolLoading, setScontrolLoading] = useState(false);
  const [scontrolBlock, setScontrolBlock] = useState("");
  const [scontrolErr, setScontrolErr] = useState(false);

  const [tailOpen, setTailOpen] = useState(false);
  const [tailTitle, setTailTitle] = useState("");
  const [tailPayload, setTailPayload] = useState<JobStdoutTailPayload | null>(null);
  const [tailBusyJob, setTailBusyJob] = useState<string | null>(null);

  // scancel confirmation modal (KUI) — holds the job id awaiting confirmation, or null when closed.
  const [scancelJobId, setScancelJobId] = useState<string | null>(null);
  const [scancelBusy, setScancelBusy] = useState(false);

  const resetBody = useCallback(() => {
    setBannerErr("");
    setStderrNote("");
    setStdout("");
    setParsed(null);
    setCmd("");
    setScontrolJobId(null);
    setScontrolBlock("");
    setScontrolErr(false);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    resetBody();
    try {
      const { res, data } = await fetchSlurmJson("/api/scheduler-jobs");
      setCmd(data.command_display || "");
      if (!res.ok) {
        setBannerErr(data.error || `HTTP ${res.status}`);
        const bits: string[] = [];
        if (data.stdout) bits.push(String(data.stdout));
        if (data.stderr) bits.push(`stderr:\n${String(data.stderr)}`);
        setStdout(bits.join("\n\n") || "");
        setParsed({ headers: [], rows: [], useRaw: true });
        return;
      }
      if (data.ok === false) {
        setBannerErr(
          [data.error || "Job query failed", data.stderr ? `\n${String(data.stderr)}` : "", data.returncode != null ? `\n(exit ${data.returncode})` : ""]
            .filter(Boolean)
            .join("")
        );
      }
      const out = data.stdout != null ? String(data.stdout) : "";
      setStdout(out);
      const p = parseSqueueStdout(out);
      setParsed(p);
      if (data.ok && data.stderr && String(data.stderr).trim()) {
        setStderrNote(`stderr:\n${String(data.stderr).trim()}`);
      }
    } catch (e) {
      setBannerErr(String(e));
      setParsed({ headers: [], rows: [], useRaw: true });
    } finally {
      setLoading(false);
    }
  }, [resetBody]);

  useEffect(() => {
    if (!active) return;
    void load();
  }, [active, load]);

  const showScontrol = useCallback(async (jobId: string) => {
    setScontrolJobId(jobId);
    setScontrolLoading(true);
    setScontrolBlock("Loading…");
    setScontrolErr(false);
    try {
      const qs = new URLSearchParams({ job_id: jobId });
      const { res, data } = await fetchSlurmJson(`/api/scheduler-job-detail?${qs}`);
      const head =
        (data.error && (!res.ok || data.ok === false) ? `${String(data.error)}\n\n` : "") +
        (data.command_display ? `${String(data.command_display)}\n\n` : "");
      const detail = formatSlurmCmdDetail(data);
      setScontrolBlock(head + detail);
      setScontrolErr(!(res.ok && data.ok !== false));
    } catch (e) {
      setScontrolBlock(String(e));
      setScontrolErr(true);
    } finally {
      setScontrolLoading(false);
    }
  }, []);

  const openJobLogTail = useCallback(
    async (jobId: string) => {
      setTailBusyJob(jobId);
      try {
        const qs = new URLSearchParams({ job_id: jobId });
        const { res, data } = await fetchSlurmJson(`/api/scheduler-job-detail?${qs}`);
        const showBlock = String(data.stdout || "") + (data.stderr ? `\nstderr:\n${data.stderr}` : "");
        if (!res.ok || data.ok === false) {
          window.alert((data.error || "Job details query failed") + "\n\n" + formatSlurmCmdDetail(data));
          return;
        }
        const path = String(data.path || "").trim() || parseSlurmStdOutPathFromScontrolText(showBlock);
        if (!path) {
          window.alert(
            `No usable output path for PythonCM command ${jobId}. Use the command id link to inspect ProgramRunner status.`
          );
          return;
        }
        const d = (logDir ?? "").trim();
        const payload: JobStdoutTailPayload = d ? { path, log_dir: d } : { path };
        setTailTitle(`tail -f — ${path}`);
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

  // Runs after the user confirms in the KUI modal. Errors surface in the panel banner (no browser alert).
  const doScancel = useCallback(
    async (jobId: string) => {
      setScancelBusy(true);
      try {
        const res = await apiFetch("/api/scheduler-cancel", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ job_id: jobId }),
        });
        const raw = await res.text();
        let data: SlurmCmdJson = {};
        try {
          data = raw ? (JSON.parse(raw) as SlurmCmdJson) : {};
        } catch {
          setBannerErr(`Invalid JSON (HTTP ${res.status}): ${raw.slice(0, 400)}`);
          return;
        }
        const detail = formatSlurmCmdDetail(data);
        if (!res.ok || !data.ok) {
          setBannerErr(`${data.error || "Job cancellation failed"}\n\n${detail}`);
          return;
        }
        setScancelJobId(null);
        await load();
      } catch (e) {
        setBannerErr(String(e));
      } finally {
        setScancelBusy(false);
      }
    },
    [load]
  );

  const headers = parsed && !parsed.useRaw && parsed.headers.length ? parsed.headers : [];
  const rows = parsed && !parsed.useRaw && parsed.headers.length ? parsed.rows : [];
  // squeue on a 2000-node cluster is routinely hundreds of lines; the 360px scroll box hid
  // that rather than solving it. Sorting applies to every row, not just the visible page.
  const rowsPage = useSortedPagedRows(rows, cellAt);
  const jobCol = headers.length ? findSqueueJobColumnIndex(headers) : -1;
  const reasonCol = headers.findIndex((h) => /REASON/i.test(h));
  const stCol = headers.findIndex((h) => h === "ST" || /^STATE$/i.test(h));
  const showRaw = !parsed || parsed.useRaw || !headers.length;

  return (
    <Paper variant="outlined" sx={{ borderLeft: `4px solid ${NVIDIA_GREEN}`, overflow: "hidden" }}>
      <Box sx={{ px: 2, py: 1.5, borderBottom: 1, borderColor: "divider", bgcolor: "action.hover", display: "flex", alignItems: "center", gap: 1, flexWrap: "wrap" }}>
        <Text kind="body/bold/md" style={{ fontWeight: 700, color: NVIDIA_GREEN }}>
          Active jobs
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
      {stderrNote && (
        <Text kind="body/regular/md" style={{ display: "block", paddingLeft: 16, paddingRight: 16, paddingTop: 8, whiteSpace: "pre-wrap", color: "#ed6c02" }}>
          {stderrNote}
        </Text>
      )}

      {!showRaw && headers.length > 0 && (
        <TableContainer sx={{ maxHeight: tableMaxHeight }}>
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                {headers.map((h, ci) => (
                  <SortableHeaderCell
                    key={h}
                    label={h}
                    col={ci}
                    sortCol={rowsPage.sortCol}
                    sortDir={rowsPage.sortDir}
                    onToggle={rowsPage.toggleSort}
                  />
                ))}
                <TableCell sx={{ fontWeight: 700, bgcolor: "action.hover", color: NVIDIA_GREEN }}>log</TableCell>
                <TableCell sx={{ fontWeight: 700, bgcolor: "action.hover", color: NVIDIA_GREEN }}>cancel</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {rowsPage.visible.map((cells, ri) => {
                const rawJob = jobCol >= 0 && cells[jobCol] != null ? String(cells[jobCol]) : "";
                const jid = schedulerJobIdForAction(rawJob);
                return (
                  <TableRow key={ri} hover sx={{ "&:nth-of-type(even)": { bgcolor: "action.hover" } }}>
                    {headers.map((_, ci) => {
                      const value = cells[ci] ?? "";
                      const isReason = ci === reasonCol;
                      // Color the reason (and ST) cell by severity so launch/prolog failures pop.
                      const toneSource = isReason || ci === stCol ? value : "";
                      const tone = toneSource ? slurmReasonTone(toneSource) : "none";
                      const toneColor = slurmToneColor(tone);
                      return (
                        <TableCell
                          key={ci}
                          title={value}
                          sx={{
                            fontFamily: "monospace",
                            fontSize: 12,
                            whiteSpace: isReason ? "normal" : "nowrap",
                            maxWidth: isReason ? 360 : 240,
                            overflow: isReason ? "visible" : "hidden",
                            textOverflow: isReason ? "clip" : "ellipsis",
                            wordBreak: isReason ? "break-word" : "normal",
                            color: toneColor,
                            fontWeight: toneColor ? 600 : undefined,
                          }}
                        >
                          {ci === jobCol && jid ? (
                            <Link
                              component="button"
                              type="button"
                              underline="always"
                              onClick={() => void showScontrol(jid)}
                              sx={{ fontFamily: "inherit", fontSize: "inherit", cursor: "pointer" }}
                              title={`Show scheduler details for job ${jid}`}
                            >
                              {rawJob}
                            </Link>
                          ) : (
                            value
                          )}
                        </TableCell>
                      );
                    })}
                    <TableCell sx={{ whiteSpace: "nowrap" }}>
                      {jid ? (
                        <Button
                          size="small"
                          kind="secondary"
                          onClick={() => void openJobLogTail(jid)}
                          disabled={tailBusyJob === jid}
                          title="Live tail of job StdOut= path (same as legacy open log)"
                        >
                          open log
                        </Button>
                      ) : null}
                    </TableCell>
                    <TableCell sx={{ whiteSpace: "nowrap" }}>
                      {jid ? (
                        <Button size="small" kind="primary" color="danger" onClick={() => setScancelJobId(jid)} title={`Cancel job ${jid}`}>
                          cancel
                        </Button>
                      ) : null}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableContainer>
      )}
      {!showRaw && headers.length > 0 && (
        <Box sx={{ px: 2, pb: 1 }}>
          <TablePager {...rowsPage} noun="jobs" compact />
        </Box>
      )}

      {showRaw && !loading && (
        <Box
          component="pre"
          sx={{
            m: 0,
            p: 2,
            maxHeight: tableMaxHeight,
            overflow: "auto",
            fontSize: 12,
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          {stdout || "(no output)"}
        </Box>
      )}

      {scontrolJobId && (
        <Box
          sx={{
            px: 2,
            py: 1.5,
            borderTop: 1,
            borderColor: "divider",
            bgcolor: scontrolErr ? "error.dark" : "action.hover",
          }}
        >
          <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 4, fontWeight: 600 }}>
            Scheduler details — {scontrolJobId}
          </Text>
          {scontrolLoading && (
            <Box sx={{ mb: 1 }}>
              <LinearProgress />
            </Box>
          )}
          <Box
            component="pre"
            sx={{
              m: 0,
              maxHeight: 220,
              overflow: "auto",
              fontSize: 11,
              fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
            }}
          >
            {scontrolBlock}
          </Box>
          <Button size="small" kind="secondary" onClick={() => setScontrolJobId(null)} style={{ marginTop: 8 }}>
            Hide details
          </Button>
        </Box>
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

      <Modal
        open={scancelJobId !== null}
        onOpenChange={(o) => {
          if (!o && !scancelBusy) setScancelJobId(null);
        }}
        modal={true}
        style={{ width: "460px", maxWidth: "90vw" }}
        slotHeading={
          <>
            <XCircle size={18} color="var(--text-color-error, #f44336)" />
            Cancel job {scancelJobId}?
          </>
        }
        slotFooter={
          <>
            <ModalCloseButton kind="secondary" disabled={scancelBusy}>
              Keep job
            </ModalCloseButton>
            <Button
              kind="primary"
              color="danger"
              onClick={() => {
                if (scancelJobId) void doScancel(scancelJobId);
              }}
              disabled={scancelBusy || !scancelJobId}
            >
              {scancelBusy ? "Cancelling…" : "Cancel job"}
            </Button>
          </>
        }
      >
        <Flex direction="col" gap="2">
          <Text kind="body/regular/md">
            This immediately terminates the job and any running steps. This cannot be undone.
          </Text>
          <Text
            kind="mono/sm"
            style={{
              display: "block",
              padding: "8px 10px",
              borderRadius: 6,
              background: "var(--surface-color, #1a1a1a)",
              color: "var(--text-color-secondary)",
              wordBreak: "break-all",
            }}
          >
            Scheduler cancellation request for job {scancelJobId}
          </Text>
        </Flex>
      </Modal>
    </Paper>
  );
}
