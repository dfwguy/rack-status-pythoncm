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
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import Button from "@mui/material/Button";
import Typography from "@mui/material/Typography";
import Box from "@mui/material/Box";
import { useEffect, useRef } from "react";

export interface ToolsLiveOutputDialogProps {
  open: boolean;
  title: string;
  /** Full streamed log (UTF-8 text). */
  logText: string;
  /** When true, a stream is still in progress for this tool. */
  running?: boolean;
  onClose: () => void;
}

export function ToolsLiveOutputDialog({ open, title, logText, running = false, onClose }: ToolsLiveOutputDialogProps) {
  const preRef = useRef<HTMLPreElement>(null);
  // Follow the tail by default, but stop auto-scrolling once the user scrolls up to read
  // (re-engages when they scroll back near the bottom). Avoids yanking the viewport on
  // every streamed update.
  const stickToBottomRef = useRef(true);

  const handleScroll = () => {
    const el = preRef.current;
    if (!el) return;
    stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  useEffect(() => {
    const el = preRef.current;
    if (!open || !el) return;
    if (stickToBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [logText, open, running]);

  useEffect(() => {
    if (open) stickToBottomRef.current = true;
  }, [open]);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth aria-labelledby="tools-live-output-title">
      <DialogTitle id="tools-live-output-title">{title}</DialogTitle>
      <DialogContent>
        {running ? (
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
            Streaming server output…
          </Typography>
        ) : null}
        <Box
          component="pre"
          ref={preRef}
          onScroll={handleScroll}
          sx={{
            m: 0,
            p: 1.5,
            maxHeight: "min(70vh, 520px)",
            overflow: "auto",
            bgcolor: "action.hover",
            borderRadius: 1,
            fontSize: "0.75rem",
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          {logText || "(no output yet)"}
        </Box>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant="contained" color="inherit">
          Close
        </Button>
      </DialogActions>
    </Dialog>
  );
}
