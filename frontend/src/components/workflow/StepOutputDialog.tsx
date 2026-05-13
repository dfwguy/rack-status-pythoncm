import { useState, useEffect } from "react";
import { Modal, ModalCloseButton, Text, Flex, Tag } from "@nvidia/foundations-react-core";
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
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      modal={true}
      style={{ width: "700px", maxWidth: "90vw" }}
      slotHeading={
        <>
          <Terminal size={18} />
          Step Output — {stepName}
        </>
      }
      slotFooter={
        <ModalCloseButton kind="tertiary">Close</ModalCloseButton>
      }
    >
      <Flex direction="col" gap="3">
        {/* Status summary */}
        <Flex gap="2" align="center">
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
        </Flex>

        {/* Tabs */}
        <Tabs value={tab} onChange={handleTabChange} sx={{ borderBottom: 1, borderColor: "divider" }}>
          <Tab label="Summary" icon={<FileText size={14} />} iconPosition="start" sx={{ textTransform: "none", minHeight: 40 }} />
          <Tab label="Full Output" icon={<Terminal size={14} />} iconPosition="start" sx={{ textTransform: "none", minHeight: 40 }} />
        </Tabs>

        {/* Summary tab */}
        {tab === 0 && (
          <Box>
            <Text kind="body/bold/sm" style={{ marginBottom: 8, display: "block" }}>Output Snippet (last 20 lines)</Text>
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
                maxHeight: 300,
                overflow: "auto",
              }}
            >
              {historyEntry?.output_snippet || "[No output captured]"}
            </Box>
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
                  maxHeight: 400,
                  overflow: "auto",
                }}
              >
                {fullOutput || "[No output available]"}
              </Box>
            )}
          </Box>
        )}
      </Flex>
    </Modal>
  );
}
