import { useState, useEffect } from "react";
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

  useEffect(() => {
    if (!open) return;
    setTab(0);
    setFullOutput("");
    fetchFullOutput();
  }, [open, stepId]);

  const fetchFullOutput = async () => {
    if (fullOutput) return;
    setLoadingOutput(true);
    try {
      const res = await fetch(`/api/workflow-runs/${runId}/output/${stepId}`, { credentials: "same-origin" });
      const data = await res.json();
      if (data.ok) {
        setFullOutput(data.output);
      } else {
        setFullOutput(`[Error loading output: ${data.error}]`);
      }
    } catch (e) {
      setFullOutput(`[Failed to fetch output]`);
    } finally {
      setLoadingOutput(false);
    }
  };

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
