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
import MuiButton from "@mui/material/Button";

export interface CommandTextDialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  text: string;
  isError?: boolean;
}

export function CommandTextDialog({ open, onClose, title, text, isError }: CommandTextDialogProps) {
  return (
    <Dialog open={open} onClose={onClose} maxWidth="lg" fullWidth>
      <DialogTitle sx={{ fontWeight: 600, color: isError ? "error.main" : undefined }}>{title}</DialogTitle>
      <DialogContent dividers>
        <pre
          style={{
            margin: 0,
            maxHeight: "70vh",
            overflow: "auto",
            fontSize: 12,
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
          }}
        >
          {text}
        </pre>
      </DialogContent>
      <DialogActions>
        <MuiButton variant="outlined" onClick={onClose}>Close</MuiButton>
      </DialogActions>
    </Dialog>
  );
}
