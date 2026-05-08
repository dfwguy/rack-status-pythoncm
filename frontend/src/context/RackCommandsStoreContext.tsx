import {
  createContext,
  useContext,
  useCallback,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import Snackbar from "@mui/material/Snackbar";
import Alert from "@mui/material/Alert";
import { CommandStreamDialog } from "../components/rack/CommandStreamDialog";
import { CommandTextDialog } from "../components/rack/CommandTextDialog";
import { buildSingleRackTailBody } from "../lib/rackLog";
import { formatJobStatusText, formatSyncJsonResult } from "../lib/formatJobOutput";
import type { NodeCmdKind, RackLastAsyncOutput } from "../lib/rackCommandTypes";
import { validationRackKey } from "../lib/rackCommandTypes";

interface StreamState {
  title: string;
  endpoint: string;
  body: Record<string, unknown>;
}

export interface RackCommandsStoreValue {
  /** Last finished command text for a rack (normalized key). */
  getRackLast: (rack: string) => RackLastAsyncOutput | null;
  /** In-flight jobs started from the React UI for this rack. */
  getPendingForRack: (rack: string) => Array<{ id: string; label: string }>;
  replayLastOutput: (rack: string) => void;
  openText: (title: string, text: string, isErr?: boolean) => void;
  openStream: (title: string, endpoint: string, body: Record<string, unknown>) => void;
  runSyncJson: (rack: string, title: string, url: string, body: Record<string, unknown>) => Promise<void>;
  runAsyncReset: (rack: string, label: string, cmdKind: NodeCmdKind, url: string, body: Record<string, unknown>) => Promise<void>;
  runAsyncValidationFamily: (
    rack: string,
    label: string,
    cmdKind: NodeCmdKind,
    postUrl: string,
    statusUrl: (jobId: string) => string,
    body: Record<string, unknown>,
  ) => Promise<void>;
  openTailLogNemo: (rack: string, logDir: string, jobId?: string) => void;
}

const RackCommandsStoreContext = createContext<RackCommandsStoreValue | null>(null);

export function useRackCommandsStore(): RackCommandsStoreValue {
  const v = useContext(RackCommandsStoreContext);
  if (!v) throw new Error("useRackCommandsStore must be used under RackCommandsStoreProvider");
  return v;
}

const POLL_MS = 900;
const POLL_MAX_MS = 120 * 60 * 1000;

async function pollJobUntilTerminal(statusUrl: string): Promise<Record<string, unknown>> {
  const deadline = Date.now() + POLL_MAX_MS;
  let last: Record<string, unknown> = {};
  while (Date.now() < deadline) {
    const res = await fetch(statusUrl, { credentials: "same-origin" });
    const raw = await res.text();
    let data: Record<string, unknown> = {};
    try {
      data = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
    } catch {
      throw new Error("invalid JSON from job status");
    }
    if (!res.ok) throw new Error(String(data.error || `HTTP ${res.status}`));
    last = data;
    const ph = String(data.phase || "").toLowerCase();
    if (ph === "done" || ph === "error") return data;
    await new Promise((r) => setTimeout(r, POLL_MS));
  }
  throw new Error(
    `job poll timed out (client); last phase=${String(last.phase ?? "")}`,
  );
}

function jobFinishedOk(st: Record<string, unknown>): boolean {
  const ph = String(st.phase || "").toLowerCase();
  if (ph === "error") return false;
  if (ph === "done") return st.ok !== false;
  return false;
}

function syncCmdKindFromTitle(title: string): NodeCmdKind {
  const t = title.toLowerCase();
  if (t.includes("drain") || t.includes("undrain") || t.includes("scancel")) return "slurm";
  if (t.includes("gpu")) return "gpu";
  if (t.includes("power")) return "power";
  return "validation";
}

export function RackCommandsStoreProvider({ children }: { children: ReactNode }) {
  const [lastByRack, setLastByRack] = useState<Record<string, RackLastAsyncOutput>>({});
  const [pending, setPending] = useState<Array<{ id: string; rackKey: string; label: string }>>([]);
  const [stream, setStream] = useState<StreamState | null>(null);
  const [textDlg, setTextDlg] = useState<{ title: string; text: string; isErr?: boolean } | null>(null);
  const [snack, setSnack] = useState<{ open: boolean; msg: string }>({ open: false, msg: "" });

  const setRackOutput = useCallback((rack: string, kind: NodeCmdKind, full: string, isErr: boolean, noticeTitle?: string) => {
    const rk = validationRackKey(rack);
    if (!rk) return;
    const trimmed = full.trim();
    if (!trimmed) return;
    const preview = trimmed.slice(0, 280).trim();
    const rec: RackLastAsyncOutput = {
      cmdKind: kind,
      isErr,
      preview,
      full: trimmed,
      at: Date.now(),
      noticeTitle: noticeTitle?.trim() ?? "",
    };
    setLastByRack((prev) => ({ ...prev, [rk]: rec }));
  }, []);

  const getRackLast = useCallback(
    (rack: string) => {
      const rk = validationRackKey(rack);
      return lastByRack[rk] ?? null;
    },
    [lastByRack]
  );

  const getPendingForRack = useCallback(
    (rack: string) => {
      const rk = validationRackKey(rack);
      return pending.filter((p) => p.rackKey === rk);
    },
    [pending]
  );

  const replayLastOutput = useCallback(
    (rack: string) => {
      const rec = getRackLast(rack);
      if (!rec) return;
      setTextDlg({
        title: rec.noticeTitle?.trim() || "Last output",
        text: rec.full,
        isErr: rec.isErr,
      });
    },
    [getRackLast]
  );

  const openText = useCallback((title: string, text: string, isErr?: boolean) => {
    setTextDlg({ title, text, isErr });
  }, []);

  const openStream = useCallback((title: string, endpoint: string, body: Record<string, unknown>) => {
    setStream({ title, endpoint, body });
  }, []);

  const pushPending = useCallback((rack: string, label: string) => {
    const rk = validationRackKey(rack);
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    setPending((p) => [...p, { id, rackKey: rk, label }]);
    setSnack({ open: true, msg: `Started: ${label} (${rack})` });
    return id;
  }, []);

  const popPending = useCallback((id: string) => {
    setPending((p) => p.filter((x) => x.id !== id));
  }, []);

  const runSyncJson = useCallback(
    async (rack: string, title: string, url: string, body: Record<string, unknown>) => {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        credentials: "same-origin",
      });
      const raw = await res.text();
      let data: Record<string, unknown> = {};
      try {
        data = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
      } catch {
        const msg = raw || "(empty response)";
        openText(title, msg, !res.ok);
        setRackOutput(rack, syncCmdKindFromTitle(title), msg, !res.ok, title);
        return;
      }
      const ok = res.ok && data.ok !== false;
      const full = formatSyncJsonResult(title, rack, data);
      openText(title, full, !ok);
      setRackOutput(rack, syncCmdKindFromTitle(title), full, !ok, title);
    },
    [openText, setRackOutput]
  );

  const runAsyncReset = useCallback(
    async (rack: string, label: string, cmdKind: NodeCmdKind, url: string, body: Record<string, unknown>) => {
      const pid = pushPending(rack, label);
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          credentials: "same-origin",
        });
        const data = (await res.json()) as { ok?: boolean; job_id?: string; error?: string };
        if (!res.ok || !data.ok || !data.job_id) {
          throw new Error(data.error || "failed to start job");
        }
        const st = await pollJobUntilTerminal(`/api/reset-job-status?job_id=${encodeURIComponent(data.job_id)}`);
        const ok = jobFinishedOk(st);
        const text = formatJobStatusText(st);
        openText(`${label} — finished`, text, !ok);
        setRackOutput(rack, cmdKind, text, !ok, `${label} — finished`);
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        openText(`${label} — error`, msg, true);
        setRackOutput(rack, cmdKind, msg, true, `${label} — error`);
      } finally {
        popPending(pid);
      }
    },
    [pushPending, popPending, openText, setRackOutput]
  );

  const runAsyncValidationFamily = useCallback(
    async (
      rack: string,
      label: string,
      cmdKind: NodeCmdKind,
      postUrl: string,
      statusUrl: (jobId: string) => string,
      body: Record<string, unknown>,
    ) => {
      const pid = pushPending(rack, label);
      try {
        const res = await fetch(postUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          credentials: "same-origin",
        });
        const data = (await res.json()) as { ok?: boolean; job_id?: string; error?: string };
        if (!res.ok || !data.ok || !data.job_id) {
          throw new Error(data.error || "failed to start job");
        }
        const st = await pollJobUntilTerminal(statusUrl(data.job_id));
        const ok = jobFinishedOk(st);
        const text = formatJobStatusText(st);
        openText(`${label} — finished`, text, !ok);
        setRackOutput(rack, cmdKind, text, !ok, `${label} — finished`);
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        openText(`${label} — error`, msg, true);
        setRackOutput(rack, cmdKind, msg, true, `${label} — error`);
      } finally {
        popPending(pid);
      }
    },
    [pushPending, popPending, openText, setRackOutput]
  );

  const openTailLogNemo = useCallback((rack: string, logDir: string, jobId?: string) => {
    const ld = logDir.trim();
    const jid = jobId && jobId !== "-" ? jobId : undefined;
    const tailBody = buildSingleRackTailBody(ld || undefined, rack, jid);
    setStream({
      title: `NEMO log (tail) — ${rack}`,
      endpoint: "/api/tail-log-stream",
      body: tailBody as unknown as Record<string, unknown>,
    });
  }, []);

  const value = useMemo<RackCommandsStoreValue>(
    () => ({
      getRackLast,
      getPendingForRack,
      replayLastOutput,
      openText,
      openStream,
      runSyncJson,
      runAsyncReset,
      runAsyncValidationFamily,
      openTailLogNemo,
    }),
    [
      getRackLast,
      getPendingForRack,
      replayLastOutput,
      openText,
      openStream,
      runSyncJson,
      runAsyncReset,
      runAsyncValidationFamily,
      openTailLogNemo,
    ]
  );

  return (
    <RackCommandsStoreContext.Provider value={value}>
      {children}
      <CommandStreamDialog
        open={stream !== null}
        onClose={() => setStream(null)}
        title={stream?.title ?? ""}
        endpoint={stream?.endpoint ?? "/api/run-sysinfo-stream"}
        body={stream?.body ?? null}
      />
      <CommandTextDialog
        open={textDlg !== null}
        onClose={() => setTextDlg(null)}
        title={textDlg?.title ?? ""}
        text={textDlg?.text ?? ""}
        isError={textDlg?.isErr}
      />
      <Snackbar
        open={snack.open}
        autoHideDuration={4000}
        onClose={() => setSnack((s) => ({ ...s, open: false }))}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      >
        <Alert severity="info" onClose={() => setSnack((s) => ({ ...s, open: false }))}>
          {snack.msg}
        </Alert>
      </Snackbar>
    </RackCommandsStoreContext.Provider>
  );
}
