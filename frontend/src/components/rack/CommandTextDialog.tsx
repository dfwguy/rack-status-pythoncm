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
