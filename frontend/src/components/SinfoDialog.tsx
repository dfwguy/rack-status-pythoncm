import { useState, useEffect } from "react";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import Box from "@mui/material/Box";
import CircularProgress from "@mui/material/CircularProgress";
import { Button, Text } from "@nvidia/foundations-react-core";

interface SinfoDialogProps {
  open: boolean;
  onClose: () => void;
}

export function SinfoDialog({ open, onClose }: SinfoDialogProps) {
  const [data, setData] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError("");
    fetch("/api/slurm-sinfo-r")
      .then((res) => res.json())
      .then((json) => {
        setData(json.stdout || json.output || JSON.stringify(json, null, 2));
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [open]);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontWeight: 600 }}>sinfo -R --long</DialogTitle>
      <DialogContent dividers>
        {loading && (
          <Box sx={{ display: "flex", justifyContent: "center", py: 4 }}>
            <CircularProgress />
          </Box>
        )}
        {error && (
          <Text kind="body/regular/md" style={{ color: "var(--text-color-error, #f44336)" }}>{error}</Text>
        )}
        {!loading && !error && (
          <pre
            style={{
              fontFamily: "monospace",
              fontSize: 14,
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
              margin: 0,
              maxHeight: "60vh",
              overflow: "auto",
            }}
          >
            {data || "No output"}
          </pre>
        )}
      </DialogContent>
      <DialogActions>
        <Button kind="tertiary" onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}
