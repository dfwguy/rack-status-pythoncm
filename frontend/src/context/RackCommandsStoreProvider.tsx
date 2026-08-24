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
import {
  useCallback,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import Snackbar from "@mui/material/Snackbar";
import Alert from "@mui/material/Alert";
import { CommandStreamDialog } from "../components/rack/CommandStreamDialog";
import { CommandTextDialog } from "../components/rack/CommandTextDialog";
import { LockConflictDialog } from "../components/rack/LockConflictDialog";
import { buildSingleRackTailBody } from "../lib/rackLog";
import { formatJobStatusText, formatSyncJsonResult } from "../lib/formatJobOutput";
import type { NodeCmdKind, RackLastAsyncOutput } from "../lib/rackCommandTypes";
import { validationRackKey } from "../lib/rackCommandTypes";
import { apiFetch } from "../lib/apiFetch";
import { launchWithReview } from "../lib/launchReview";
import { useRackLocksContext } from "./RackLocksContext";
import type { RackLockEntry } from "../hooks/useRackLocks";
import {
  RackCommandsStoreContext,
  type RackCommandsStoreValue,
} from "./RackCommandsStoreContext";

interface StreamState {
  title: string;
  endpoint: string;
  body: Record<string, unknown>;
}


const POLL_MS = 900;
const POLL_MAX_MS = 120 * 60 * 1000;

async function pollJobUntilTerminal(statusUrl: string): Promise<Record<string, unknown>> {
  const deadline = Date.now() + POLL_MAX_MS;
  let last: Record<string, unknown> = {};
  while (Date.now() < deadline) {
    const res = await apiFetch(statusUrl);
    const raw = await res.text();
    // No initialiser: the catch throws, so `data` is either parsed or we never reach the use.
    let data: Record<string, unknown>;
    try {
      data = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
    } catch {
      throw new Error("invalid JSON from job status");
    }
    if (!res.ok) throw new Error(String(data.error || `HTTP ${res.status}`));
    last = data;
    const ph = String(data.phase || "").toLowerCase();
    if (ph === "done" || ph === "error" || ph === "timeout") return data;
    await new Promise((r) => setTimeout(r, POLL_MS));
  }
  throw new Error(
    `job poll timed out (client); last phase=${String(last.phase ?? "")}`,
  );
}

function jobFinishedOk(st: Record<string, unknown>): boolean {
  const ph = String(st.phase || "").toLowerCase();
  if (ph === "error" || ph === "timeout") return false;
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

  const { isLockedByOther, refresh: refreshLocks } = useRackLocksContext();

  /** When non-null, the LockConflictDialog is open with these conflicts.
   *  ``resolve`` is settled with ``true`` (run anyway) or ``false`` (cancel). */
  const [conflictPrompt, setConflictPrompt] = useState<
    | {
        conflicts: Array<{ rack: string; lock: RackLockEntry }>;
        resolve: (proceed: boolean) => void;
      }
    | null
  >(null);
  /** Avoid stacking prompts when the user fires multiple commands in quick succession. */
  const conflictResolverRef = useRef<((proceed: boolean) => void) | null>(null);

  const promptIfLocked = useCallback(
    async (racks: string[]): Promise<boolean> => {
      const seen = new Set<string>();
      const conflicts: Array<{ rack: string; lock: RackLockEntry }> = [];
      for (const raw of racks) {
        const r = (raw || "").trim();
        if (!r || seen.has(r)) continue;
        seen.add(r);
        const lock = isLockedByOther(r);
        if (lock) conflicts.push({ rack: r, lock });
      }
      if (conflicts.length === 0) return true;
      return new Promise<boolean>((resolve) => {
        conflictResolverRef.current = resolve;
        setConflictPrompt({ conflicts, resolve });
      });
    },
    [isLockedByOther],
  );

  const handleConflictCancel = useCallback(() => {
    const r = conflictResolverRef.current;
    conflictResolverRef.current = null;
    setConflictPrompt(null);
    r?.(false);
  }, []);

  const handleConflictProceed = useCallback(() => {
    const r = conflictResolverRef.current;
    conflictResolverRef.current = null;
    setConflictPrompt(null);
    r?.(true);
    // Re-poll so the optimistic UI catches up quickly with whatever the operator did.
    void refreshLocks();
  }, [refreshLocks]);

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

  const openStream = useCallback(
    async (title: string, endpoint: string, body: Record<string, unknown>) => {
      const rack = typeof body?.rack === "string" ? (body.rack as string) : "";
      // Only gate write-y streams that target a specific rack (skip read-only tail streams).
      if (rack && /sysinfo|nvswitch-audit/.test(endpoint)) {
        const proceed = await promptIfLocked([rack]);
        if (!proceed) return;
      }
      setStream({ title, endpoint, body });
    },
    [promptIfLocked],
  );

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
      const proceed = await promptIfLocked([rack]);
      if (!proceed) return;
      const res = await apiFetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const raw = await res.text();
      // No initialiser: the catch returns, so `data` is either parsed or we never use it.
      let data: Record<string, unknown>;
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
    [openText, setRackOutput, promptIfLocked]
  );

  const runAsyncReset = useCallback(
    async (rack: string, label: string, cmdKind: NodeCmdKind, url: string, body: Record<string, unknown>) => {
      const proceed = await promptIfLocked([rack]);
      if (!proceed) return;
      const pid = pushPending(rack, label);
      try {
        const res = await apiFetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
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
    [pushPending, popPending, openText, setRackOutput, promptIfLocked]
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
      const proceed = await promptIfLocked([rack]);
      if (!proceed) return;
      const pid = pushPending(rack, label);
      try {
        // prepare -> digest -> launch, so the target the operator reviewed is the target that
        // runs (REQ-6). A no-op for a URL outside LAUNCH_KIND_BY_URL.
        const res = await launchWithReview(postUrl, body);
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
    [pushPending, popPending, openText, setRackOutput, promptIfLocked]
  );

  const launchAsyncValidationFamily = useCallback(
    async (
      rack: string,
      label: string,
      cmdKind: NodeCmdKind,
      postUrl: string,
      statusUrl: (jobId: string) => string,
      body: Record<string, unknown>,
    ): Promise<string> => {
      const proceed = await promptIfLocked([rack]);
      if (!proceed) throw new Error("Launch cancelled; no validation job was submitted.");

      let jobId = "";
      try {
        // prepare -> digest -> launch, so the target the operator reviewed is the target that
        // runs (REQ-6). A no-op for a URL outside LAUNCH_KIND_BY_URL.
        const res = await launchWithReview(postUrl, body);
        const data = (await res.json()) as { ok?: boolean; job_id?: string; error?: string };
        if (!res.ok || !data.ok || !data.job_id) {
          throw new Error(data.error || "failed to start job");
        }
        jobId = data.job_id;
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        setRackOutput(rack, cmdKind, message, true, `${label} — launch error`);
        throw error;
      }

      const pid = pushPending(rack, label);
      void (async () => {
        try {
          const status = await pollJobUntilTerminal(statusUrl(jobId));
          const ok = jobFinishedOk(status);
          const output = formatJobStatusText(status);
          openText(`${label} — finished`, output, !ok);
          setRackOutput(rack, cmdKind, output, !ok, `${label} — finished`);
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          openText(`${label} — error`, message, true);
          setRackOutput(rack, cmdKind, message, true, `${label} — error`);
        } finally {
          popPending(pid);
        }
      })();
      return jobId;
    },
    [openText, popPending, promptIfLocked, pushPending, setRackOutput],
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
      launchAsyncValidationFamily,
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
      launchAsyncValidationFamily,
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
      <LockConflictDialog
        open={conflictPrompt !== null}
        conflicts={conflictPrompt?.conflicts ?? []}
        onCancel={handleConflictCancel}
        onProceed={handleConflictProceed}
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
