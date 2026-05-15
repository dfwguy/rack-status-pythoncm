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
import { Text, Button } from "@nvidia/foundations-react-core";
import {
  fetchSlurmJson,
  findSqueueJobColumnIndex,
  formatSlurmCmdDetail,
  parseSlurmStdOutPathFromScontrolText,
  slurmJobIdForScancel,
  type SlurmCmdJson,
} from "../lib/slurmHelpers";
import { parseSqueueStdout } from "../lib/parseSqueueStdout";
import type { JobStdoutTailPayload } from "../lib/rackLog";
import { TailLogDialog } from "./TailLogDialog";

const NVIDIA_GREEN = "#76B900";

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
      const { res, data } = await fetchSlurmJson("/api/slurm-squeue");
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
          [data.error || "squeue failed", data.stderr ? `\n${String(data.stderr)}` : "", data.returncode != null ? `\n(exit ${data.returncode})` : ""]
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
      const { res, data } = await fetchSlurmJson(`/api/slurm-scontrol-show-job?${qs}`);
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
        const { res, data } = await fetchSlurmJson(`/api/slurm-scontrol-show-job?${qs}`);
        const showBlock = String(data.stdout || "") + (data.stderr ? `\nstderr:\n${data.stderr}` : "");
        if (!res.ok || data.ok === false) {
          window.alert((data.error || "scontrol show job failed") + "\n\n" + formatSlurmCmdDetail(data));
          return;
        }
        const path = parseSlurmStdOutPathFromScontrolText(showBlock);
        if (!path) {
          window.alert(
            `No usable StdOut= path for job ${jobId} (missing, (none), or /dev/null). Use the job id link to inspect scontrol output.`
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

  const runScancel = useCallback(
    async (jobId: string) => {
      if (
        !window.confirm(`Cancel Slurm job ${jobId}?\n\nmodule load slurm && scancel ${jobId}`)
      ) {
        return;
      }
      try {
        const res = await fetch("/api/slurm-scancel", {
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
          window.alert(`Invalid JSON (HTTP ${res.status}): ${raw.slice(0, 400)}`);
          return;
        }
        const detail = formatSlurmCmdDetail(data);
        if (!res.ok || !data.ok) {
          window.alert(`${data.error || "scancel failed"}\n\n${detail}`);
          return;
        }
        await load();
      } catch (e) {
        window.alert(String(e));
      }
    },
    [load]
  );

  const headers = parsed && !parsed.useRaw && parsed.headers.length ? parsed.headers : [];
  const rows = parsed && !parsed.useRaw && parsed.headers.length ? parsed.rows : [];
  const jobCol = headers.length ? findSqueueJobColumnIndex(headers) : -1;
  const showRaw = !parsed || parsed.useRaw || !headers.length;

  return (
    <Paper variant="outlined" sx={{ borderLeft: `4px solid ${NVIDIA_GREEN}`, overflow: "hidden" }}>
      <Box sx={{ px: 2, py: 1.5, borderBottom: 1, borderColor: "divider", bgcolor: "action.hover", display: "flex", alignItems: "center", gap: 1, flexWrap: "wrap" }}>
        <Text kind="body/bold/md" style={{ fontWeight: 700, color: NVIDIA_GREEN }}>
          squeue
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
                {headers.map((h) => (
                  <TableCell key={h} sx={{ fontWeight: 700, bgcolor: "action.hover", color: NVIDIA_GREEN, whiteSpace: "nowrap" }}>
                    {h}
                  </TableCell>
                ))}
                <TableCell sx={{ fontWeight: 700, bgcolor: "action.hover", color: NVIDIA_GREEN }}>log</TableCell>
                <TableCell sx={{ fontWeight: 700, bgcolor: "action.hover", color: NVIDIA_GREEN }}>scancel</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {rows.map((cells, ri) => {
                const rawJob = jobCol >= 0 && cells[jobCol] != null ? String(cells[jobCol]) : "";
                const jid = slurmJobIdForScancel(rawJob);
                return (
                  <TableRow key={ri} hover sx={{ "&:nth-of-type(even)": { bgcolor: "action.hover" } }}>
                    {headers.map((_, ci) => (
                      <TableCell key={ci} sx={{ fontFamily: "monospace", fontSize: 12, whiteSpace: "nowrap", maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis" }}>
                        {ci === jobCol && jid ? (
                          <Link
                            component="button"
                            type="button"
                            underline="always"
                            onClick={() => void showScontrol(jid)}
                            sx={{ fontFamily: "inherit", fontSize: "inherit", cursor: "pointer" }}
                            title={`scontrol show job ${jid} (output below table)`}
                          >
                            {rawJob}
                          </Link>
                        ) : (
                          (cells[ci] ?? "")
                        )}
                      </TableCell>
                    ))}
                    <TableCell sx={{ whiteSpace: "nowrap" }}>
                      {jid ? (
                        <Button
                          size="small"
                          kind="tertiary"
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
                        <Button size="small" kind="tertiary" color="danger" onClick={() => void runScancel(jid)} title={`scancel ${jid}`}>
                          scancel
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
            scontrol show job — {scontrolJobId}
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
          <Button size="small" kind="tertiary" onClick={() => setScontrolJobId(null)} style={{ marginTop: 8 }}>
            Hide scontrol
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
    </Paper>
  );
}
