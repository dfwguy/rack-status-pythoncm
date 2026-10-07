// The operation-log viewer: the scheduler's JobLogContentDialog construct pointed at the P2P
// operations surface. A one-shot read of the worker's log for a finished (or running) operation,
// shown in-app rather than opened as a bare URL in a new tab.
import { useEffect, useRef, useState } from "react";

import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Box from "@mui/material/Box";
import { Button, Text } from "@nvidia/foundations-react-core";

import { fetchOperationLog } from "../services/fleetService";
import type { Operation } from "../types/fleet";

export interface OperationLogDialogProps {
  open: boolean;
  onClose: () => void;
  operation: Operation | null;
}

export function OperationLogDialog({ open, onClose, operation }: OperationLogDialogProps) {
  const preRef = useRef<HTMLPreElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [tooLarge, setTooLarge] = useState<number | null>(null);

  useEffect(() => {
    if (!open || !operation) return;
    const ac = new AbortController();
    /* eslint-disable react-hooks/set-state-in-effect */
    setBusy(true);
    setError("");
    setTooLarge(null);
    /* eslint-enable react-hooks/set-state-in-effect */
    if (preRef.current) preRef.current.textContent = "";

    (async () => {
      try {
        const log = await fetchOperationLog(operation.id);
        if (ac.signal.aborted) return;
        if (!log.ok) {
          setError(log.error || "No log available.");
          return;
        }
        if (log.too_large) {
          setTooLarge(log.size_bytes ?? 0);
          return;
        }
        if (preRef.current) preRef.current.textContent = log.content ?? "";
      } catch (e: unknown) {
        const err = e as { name?: string };
        if (err?.name !== "AbortError") setError(String(e));
      } finally {
        if (!ac.signal.aborted) setBusy(false);
      }
    })();

    return () => ac.abort();
  }, [open, operation]);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="lg" fullWidth>
      <DialogTitle sx={{ fontWeight: 600 }}>
        {operation ? `${operation.action} — ${operation.id}` : "Operation log"}
      </DialogTitle>
      <DialogContent dividers>
        {busy && <Text kind="body/regular/md">Loading…</Text>}
        {error && <Text kind="body/regular/md" style={{ color: "var(--color-text-danger, #c00)" }}>{error}</Text>}
        {tooLarge !== null && (
          <Text kind="body/regular/md">
            Log is {Math.round(tooLarge / 1024)} KB — too large to show inline. It is on the app
            server at the operation&rsquo;s {".log"} file.
          </Text>
        )}
        {!busy && !error && tooLarge === null && (
          <Box
            component="pre"
            ref={preRef}
            sx={{
              m: 0, p: 1.5, whiteSpace: "pre-wrap", wordBreak: "break-word",
              fontFamily: "ui-monospace, monospace", fontSize: 13, maxHeight: "60vh", overflow: "auto",
              background: "var(--color-surface-secondary, #f5f5f5)", borderRadius: 1,
            }}
          />
        )}
      </DialogContent>
      <DialogActions>
        <Button kind="secondary" color="neutral" size="small" onClick={onClose}>
          Close
        </Button>
      </DialogActions>
    </Dialog>
  );
}
