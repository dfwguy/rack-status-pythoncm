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
import { useLayoutEffect, useRef, useState } from "react";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import MuiButton from "@mui/material/Button";
import Box from "@mui/material/Box";
import LinearProgress from "@mui/material/LinearProgress";
import { apiFetch } from "../../lib/apiFetch";

export interface CommandStreamDialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  /** POST endpoint e.g. `/api/run-sysinfo-stream` */
  endpoint: string;
  /** JSON body; when null while open, stream is skipped. */
  body: Record<string, unknown> | null;
}

/**
 * Generic POST streaming dialog (sysinfo, nvswitch audit, tail, etc.) — same reader pattern as TailLogDialog.
 */
export function CommandStreamDialog({ open, onClose, title, endpoint, body }: CommandStreamDialogProps) {
  const preRef = useRef<HTMLPreElement>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");

  useLayoutEffect(() => {
    if (!open || !body) return;

    const clearPre = () => {
      const el = preRef.current;
      if (el) el.textContent = "";
    };
    const setPreText = (text: string) => {
      const el = preRef.current;
      if (!el) return;
      el.textContent = text;
      el.scrollTop = el.scrollHeight;
    };
    const appendPre = (chunk: string) => {
      const el = preRef.current;
      if (!el || !chunk) return;
      el.textContent += chunk;
      el.scrollTop = el.scrollHeight;
    };

    clearPre();
    setBusy(true);
    setStatus("Connecting…");
    const ac = new AbortController();

    (async () => {
      // Dialog portal may attach <pre> after first paint; wait until ref exists.
      for (let i = 0; i < 30 && !preRef.current; i++) {
        await new Promise<void>((r) => requestAnimationFrame(() => r()));
      }
      try {
        const res = await apiFetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: ac.signal,
        });
        if (res.status === 401) {
          // apiFetch already tried a renewal and, on failure, signed the UI out.
          setPreText("Your session has expired. Sign in again to continue.\n");
          return;
        }
        if (!res.ok) {
          const t = await res.text();
          let msg = t;
          try {
            const j = JSON.parse(t) as { error?: string };
            if (j?.error) msg = j.error;
          } catch {
            /* keep text */
          }
          setPreText(msg);
          setStatus("");
          return;
        }
        if (!res.body?.getReader) {
          setPreText("Streaming is not supported in this browser.\n");
          setStatus("");
          return;
        }
        setStatus("Receiving output…");
        const reader = res.body.getReader();
        const dec = new TextDecoder("utf-8");
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          if (value?.length) appendPre(dec.decode(value, { stream: true }));
        }
        appendPre(dec.decode());
      } catch (e: unknown) {
        const err = e as { name?: string };
        if (err?.name === "AbortError") {
          appendPre("\n(stopped)\n");
        } else {
          appendPre(`\n${String(e)}\n`);
        }
      } finally {
        setBusy(false);
        setStatus("");
      }
    })();

    return () => {
      ac.abort();
    };
  }, [open, endpoint, body]);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="lg" fullWidth>
      <DialogTitle sx={{ fontWeight: 600 }}>{title}</DialogTitle>
      <DialogContent dividers>
        {busy && status && (
          <Box sx={{ mb: 1 }}>
            <LinearProgress />
            <Box sx={{ mt: 0.5, fontSize: 12, color: "text.secondary" }}>{status}</Box>
          </Box>
        )}
        <Box
          component="pre"
          ref={preRef}
          sx={{
            m: 0,
            minHeight: "12rem",
            maxHeight: "65vh",
            overflow: "auto",
            fontSize: 12,
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            color: "text.primary",
          }}
        />
      </DialogContent>
      <DialogActions>
        <MuiButton variant="outlined" onClick={onClose}>Close</MuiButton>
      </DialogActions>
    </Dialog>
  );
}
