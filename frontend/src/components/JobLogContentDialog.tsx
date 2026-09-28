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
import { useEffect, useRef, useState } from "react";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import MuiButton from "@mui/material/Button";
import Box from "@mui/material/Box";
import LinearProgress from "@mui/material/LinearProgress";
import { apiFetch, openApiUrl } from "../lib/apiFetch";

interface StdoutContentResponse {
  ok: boolean;
  too_large?: boolean;
  size_bytes?: number;
  filename?: string;
  content?: string;
  error?: string;
}

export interface JobLogContentDialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  /** Allowlisted path on the execution host; null/empty = idle. */
  path: string | null;
  logDir?: string;
}

function downloadHref(path: string, logDir?: string): string {
  const p = new URLSearchParams({ path });
  const d = (logDir ?? "").trim();
  if (d) p.set("log_dir", d);
  return `/api/scheduler-job-output-download?${p}`;
}

/**
 * Shows the WHOLE contents of a completed job stdout file (static, one-shot read
 * via /api/scheduler-job-output) rather than a tail. Use this for finished
 * results; for a job still being written use TailLogDialog (live `tail -f`).
 * Files over the inline cap fall back to a download link.
 */
export function JobLogContentDialog({ open, onClose, title, path, logDir }: JobLogContentDialogProps) {
  const preRef = useRef<HTMLPreElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [tooLarge, setTooLarge] = useState<{ size: number } | null>(null);

  useEffect(() => {
    if (!open || !path) return;
    const ac = new AbortController();
    // Fetch-on-open: reset to the loading state before the one-shot read. This is
    // the sanctioned "synchronize with an external system" effect (mirrors
    // TailLogDialog); the state is owned here, not duplicated by the parent.
    /* eslint-disable react-hooks/set-state-in-effect */
    setBusy(true);
    setError("");
    setTooLarge(null);
    /* eslint-enable react-hooks/set-state-in-effect */
    if (preRef.current) preRef.current.textContent = "";

    (async () => {
      try {
        const params = new URLSearchParams({ path });
        const d = (logDir ?? "").trim();
        if (d) params.set("log_dir", d);
        const res = await apiFetch(`/api/scheduler-job-output?${params}`, {
          credentials: "same-origin",
          signal: ac.signal,
        });
        if (res.status === 401) {
          // apiFetch already tried a renewal and, on failure, signed the UI out (the route is kept).
          return;
        }
        const json = (await res.json()) as StdoutContentResponse;
        if (!res.ok || !json.ok) {
          setError(json.error || `HTTP ${res.status}`);
          return;
        }
        if (json.too_large) {
          setTooLarge({ size: json.size_bytes ?? 0 });
          return;
        }
        if (preRef.current) preRef.current.textContent = json.content ?? "";
      } catch (e: unknown) {
        const err = e as { name?: string };
        if (err?.name !== "AbortError") setError(String(e));
      } finally {
        setBusy(false);
      }
    })();

    return () => ac.abort();
  }, [open, path, logDir]);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="lg" fullWidth>
      <DialogTitle sx={{ fontWeight: 600 }}>{title}</DialogTitle>
      <DialogContent dividers>
        {busy && (
          <Box sx={{ mb: 1 }}>
            <LinearProgress aria-label="Loading log" />
          </Box>
        )}
        {error && (
          <Box sx={{ mb: 1, fontSize: 13, color: "error.main" }}>{error}</Box>
        )}
        {tooLarge && path && (
          <Box sx={{ mb: 1, fontSize: 13, color: "text.secondary" }}>
            File is {Math.round(tooLarge.size / 1024)} KB, too large to display inline.{" "}
            <a
              href={downloadHref(path, logDir)}
              target="_blank"
              rel="noopener noreferrer"
              // A plain link cannot renew an expired session. Renew first, then let the browser
              // stream the download itself: these are the files too large to buffer in a blob.
              onClick={(e) => {
                e.preventDefault();
                void openApiUrl(downloadHref(path, logDir));
              }}
            >
              Download the full log
            </a>
            .
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
