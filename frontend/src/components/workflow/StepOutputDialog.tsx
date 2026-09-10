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
import { Text, Tag } from "@nvidia/foundations-react-core";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import MuiButton from "@mui/material/Button";
import Box from "@mui/material/Box";
import Tabs from "@mui/material/Tabs";
import Tab from "@mui/material/Tab";
import { Terminal, FileText, CheckCircle, XCircle } from "lucide-react";
import { apiFetch } from "../../lib/apiFetch";

interface StepHistoryEntry {
  step_id: string;
  type?: string;
  success?: boolean;
  output_snippet?: string;
  output_file?: string;
  completed_at?: string;
}

interface StepOutputDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  stepId: string;
  stepName: string;
  runId: string;
  historyEntry: StepHistoryEntry | null;
}

export default function StepOutputDialog({
  open,
  onOpenChange,
  stepId,
  stepName,
  runId,
  historyEntry,
}: StepOutputDialogProps) {
  const [tab, setTab] = useState(0);
  const [fullOutput, setFullOutput] = useState("");
  const [loadingOutput, setLoadingOutput] = useState(false);

  /**
   * Fetch this step's full output.
   *
   * Declared before the effect that calls it, and without the old `if (fullOutput) return`
   * guard. That guard read a STALE closure: the effect called `setFullOutput("")` and then
   * invoked this synchronously, so `fullOutput` was still the PREVIOUS step's text and the
   * fetch was skipped — reopening the dialog on another step showed the wrong pane. Callers
   * that only want to load once (the tab handler) check before calling instead.
   */
  const fetchFullOutput = useCallback(
    async (signal?: AbortSignal) => {
      // Clearing the previous step's text is part of starting a load, so it belongs here rather
      // than in the effect body.
      setLoadingOutput(true);
      setFullOutput("");
      try {
        const res = await apiFetch(`/api/workflow-runs/${runId}/output/${stepId}`, {
          credentials: "same-origin",
          signal,
        });
        const data = await res.json();
        if (signal?.aborted) return;
        setFullOutput(data.ok ? data.output : `[Error loading output: ${data.error}]`);
      } catch {
        if (signal?.aborted) return;
        setFullOutput(`[Failed to fetch output]`);
      } finally {
        if (!signal?.aborted) setLoadingOutput(false);
      }
    },
    [runId, stepId],
  );

  useEffect(() => {
    if (!open) return;
    const ac = new AbortController();
    // The loader owns the state changes (see fetchFullOutput); the effect only starts it and
    // aborts on close, so a late response cannot overwrite a newer step's output.
    void fetchFullOutput(ac.signal);
    return () => ac.abort();
  }, [open, stepId, fetchFullOutput]);

  const handleTabChange = (_: unknown, newTab: number) => {
    setTab(newTab);
    if (newTab === 1 && !fullOutput) {
      fetchFullOutput();
    }
  };

  const success = historyEntry?.success !== false;

  return (
    <Dialog
      open={open}
      onClose={() => onOpenChange(false)}
      maxWidth="md"
      fullWidth
      sx={{ zIndex: 1500 }}
    >
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <Terminal size={18} />
        Step Output — {stepName}
      </DialogTitle>
      <DialogContent sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
        {/* Status summary */}
        <Box sx={{ display: "flex", alignItems: "center", gap: 1, mt: 1 }}>
          {success ? (
            <Tag color="green" kind="solid" readOnly density="compact">
              <CheckCircle size={12} style={{ marginRight: 4 }} /> Passed
            </Tag>
          ) : (
            <Tag color="red" kind="solid" readOnly density="compact">
              <XCircle size={12} style={{ marginRight: 4 }} /> Failed
            </Tag>
          )}
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            {historyEntry?.type || "step"} | Completed: {historyEntry?.completed_at ? new Date(historyEntry.completed_at).toLocaleString() : "—"}
          </Text>
        </Box>

        {/* Tabs */}
        <Tabs value={tab} onChange={handleTabChange} sx={{ borderBottom: 1, borderColor: "divider" }}>
          <Tab label="Summary" icon={<FileText size={14} />} iconPosition="start" sx={{ textTransform: "none", minHeight: 40 }} />
          <Tab label="Full Output" icon={<Terminal size={14} />} iconPosition="start" sx={{ textTransform: "none", minHeight: 40 }} />
        </Tabs>

        {/* Summary tab */}
        {tab === 0 && (
          <Box
            sx={{
              bgcolor: "#0d1117",
              border: "1px solid",
              borderColor: "divider",
              borderRadius: 1,
              p: 2,
              fontFamily: "ui-monospace, monospace",
              fontSize: 13,
              lineHeight: 1.5,
              color: "#e6edf3",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              maxHeight: 500,
              overflow: "auto",
            }}
          >
            {historyEntry?.output_snippet || "[No output captured]"}
          </Box>
        )}

        {/* Full output tab */}
        {tab === 1 && (
          <Box>
            {loadingOutput ? (
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>Loading full output...</Text>
            ) : (
              <Box
                sx={{
                  bgcolor: "#0d1117",
                  border: "1px solid",
                  borderColor: "divider",
                  borderRadius: 1,
                  p: 2,
                  fontFamily: "ui-monospace, monospace",
                  fontSize: 13,
                  lineHeight: 1.5,
                  color: "#e6edf3",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                  maxHeight: 500,
                  overflow: "auto",
                }}
              >
                {fullOutput || "[No output available]"}
              </Box>
            )}
          </Box>
        )}
      </DialogContent>
      <DialogActions>
        <MuiButton variant="outlined" onClick={() => onOpenChange(false)}>Close</MuiButton>
      </DialogActions>
    </Dialog>
  );
}
