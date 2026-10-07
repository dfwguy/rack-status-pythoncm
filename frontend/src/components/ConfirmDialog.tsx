// A themed replacement for the browser's blocking window.confirm / window.alert. Pair it with the
// useConfirm() hook (in ./useConfirm) which drives it as a promise-based confirm()/showAlert().
// Built on MUI Dialog (no KUI equivalent) + KUI buttons, and fully theme-aware (light/dark).
// This file exports ONLY the component (+ its types) so Vite fast refresh / the
// react-refresh/only-export-components CI rule stay happy -- the hook lives next door.
import type { ReactNode } from "react";

import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Box from "@mui/material/Box";
import { Button } from "@nvidia/foundations-react-core";

export interface ConfirmOptions {
  /** Short dialog heading. Defaults to "Confirm" (or "Notice" for an alert). */
  title?: string;
  /** Body text (newlines are honored). */
  message: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** Destructive action — red confirm button, default label "Delete". */
  danger?: boolean;
  /** Acknowledge-only: hides Cancel, so the promise resolves on OK (used for alerts). */
  alert?: boolean;
}

export interface ConfirmDialogProps extends ConfirmOptions {
  open: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open, title, message, confirmLabel, cancelLabel, danger, alert, onConfirm, onCancel,
}: ConfirmDialogProps) {
  return (
    <Dialog open={open} onClose={onCancel} maxWidth="xs" fullWidth>
      <DialogTitle sx={{ fontWeight: 600 }}>{title ?? (alert ? "Notice" : "Confirm")}</DialogTitle>
      <DialogContent>
        <Box sx={{ color: "text.primary", fontSize: 14, whiteSpace: "pre-line" }}>{message}</Box>
      </DialogContent>
      <DialogActions>
        {!alert && (
          <Button kind="secondary" color="neutral" size="small" onClick={onCancel}>
            {cancelLabel ?? "Cancel"}
          </Button>
        )}
        <Button kind="primary" color={danger ? "danger" : "brand"} size="small" onClick={onConfirm}>
          {confirmLabel ?? (alert ? "OK" : danger ? "Delete" : "Confirm")}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
