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
  // A finished-or-running operation may have an empty .log (nothing written yet). Track it so the
  // viewer shows an explicit "no output" note inside the box rather than a blank, collapsed bar.
  const [empty, setEmpty] = useState(false);

  useEffect(() => {
    if (!open || !operation) return;
    const ac = new AbortController();
    /* eslint-disable react-hooks/set-state-in-effect */
    setBusy(true);
    setError("");
    setTooLarge(null);
    setEmpty(false);
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
        const content = log.content ?? "";
        if (preRef.current) preRef.current.textContent = content;
        setEmpty(content.trim().length === 0);
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
        {error && <Box sx={{ color: "error.main", fontSize: 14 }}>{error}</Box>}
        {tooLarge !== null && (
          <Text kind="body/regular/md">
            Log is {Math.round(tooLarge / 1024)} KB — too large to show inline. It is on the app
            server at the operation&rsquo;s {".log"} file.
          </Text>
        )}
        {/* The <pre> is always mounted so the imperative textContent set survives the busy->ready
            re-render -- gating it (as before) unmounted the ref mid-fetch and silently dropped the
            log. The loading / error / too-large states render as messages above it. */}
        <Box sx={{ position: "relative", mt: busy || error || tooLarge !== null ? 1 : 0 }}>
          <Box
            component="pre"
            ref={preRef}
            sx={{
              m: 0, p: 1.5, whiteSpace: "pre-wrap", wordBreak: "break-word",
              fontFamily: "ui-monospace, monospace", fontSize: 13,
              // minHeight keeps an empty log a readable rectangle (the scheduler's
              // JobLogContentDialog uses the same 12rem) instead of collapsing to a thin bar.
              minHeight: "12rem", maxHeight: "60vh", overflow: "auto",
              // Theme-aware text + surface -- a hardcoded light background left the text (which
              // inherits the dark theme's near-white colour) invisible on a white box.
              color: "text.primary", bgcolor: "action.hover", borderRadius: 1,
            }}
          />
          {empty && !busy && !error && tooLarge === null && (
            <Box
              sx={{
                position: "absolute", inset: 0, display: "flex",
                alignItems: "center", justifyContent: "center", pointerEvents: "none",
              }}
            >
              <Box sx={{ color: "text.secondary", fontSize: 14 }}>No log output yet.</Box>
            </Box>
          )}
        </Box>
      </DialogContent>
      <DialogActions>
        <Button kind="secondary" color="neutral" size="small" onClick={onClose}>
          Close
        </Button>
      </DialogActions>
    </Dialog>
  );
}
