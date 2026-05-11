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

  useEffect(() => {
    if (!open || !preRef.current) return;
    preRef.current.scrollTop = preRef.current.scrollHeight;
  }, [logText, open, running]);

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
