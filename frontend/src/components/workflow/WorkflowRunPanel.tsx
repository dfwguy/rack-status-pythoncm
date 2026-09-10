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
import { useState, useEffect, useCallback, useMemo } from "react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import MuiButton from "@mui/material/Button";
import LinearProgress from "@mui/material/LinearProgress";
import Tabs from "@mui/material/Tabs";
import Tab from "@mui/material/Tab";
import { Text, Tag, Card } from "@nvidia/foundations-react-core";
import { Play, XCircle, CheckCircle, Clock, AlertTriangle, ScrollText, GitGraph, Download } from "lucide-react";
import FlowDiagram from "./editor/FlowDiagram";
import { mermaidToReactFlow } from "./editor/mermaidToReactFlow";
import StepOutputDialog from "./StepOutputDialog";
import type { Node } from "reactflow";
import {
  getRunState,
  advanceRun,
  cancelRun,
  getWorkflow,
  type WorkflowRun,
  type WorkflowRunHistoryEntry,
  type Workflow,
} from "../../services/workflowService";
import { apiFetch } from "../../lib/apiFetch";

interface WorkflowRunPanelProps {
  runId: string;
  onClose: () => void;
}

const STATE_COLORS: Record<string, "green" | "red" | "yellow" | "blue" | "gray"> = {
  pending: "gray",
  running: "blue",
  waiting_manual: "yellow",
  waiting_decision: "yellow",
  completed: "green",
  failed: "red",
  cancelled: "gray",
};

const STATE_ICONS: Record<string, React.ReactNode> = {
  pending: <Clock size={14} />,
  running: <Play size={14} />,
  waiting_manual: <Clock size={14} />,
  waiting_decision: <Clock size={14} />,
  completed: <CheckCircle size={14} />,
  failed: <AlertTriangle size={14} />,
  cancelled: <XCircle size={14} />,
};

export default function WorkflowRunPanel({ runId, onClose }: WorkflowRunPanelProps) {
  const [run, setRun] = useState<WorkflowRun | null>(null);
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [loading, setLoading] = useState(true);
  const [decisionChoice, setDecisionChoice] = useState("");

  const loadState = useCallback(async () => {
    try {
      const r = await getRunState(runId);
      setRun(r);
      if (!workflow || workflow.id !== r.workflow_id) {
        const wf = await getWorkflow(r.workflow_id);
        setWorkflow(wf);
      }
    } catch {
      // ignore polling errors
    } finally {
      setLoading(false);
    }
  }, [runId, workflow]);

  useEffect(() => {
    loadState();
    const interval = setInterval(loadState, 3000);
    return () => clearInterval(interval);
  }, [loadState]);

  const { nodes, edges } = useMemo(() => {
    if (!workflow?.mermaid) return { nodes: [], edges: [] };
    const stepTypes: Record<string, string> = {};
    for (const s of workflow.steps || []) stepTypes[s.id] = s.type;
    try {
      return mermaidToReactFlow(workflow.mermaid, stepTypes);
    } catch {
      return { nodes: [], edges: [] };
    }
  }, [workflow]);

  const completedStepIds = useMemo(() => {
    if (!run) return [];
    if (run.state === "completed") {
      return nodes.map((n) => n.id);
    }
    return (run.history || []).map((h) => h.step_id);
  }, [run, nodes]);

  const failedStepIds = useMemo(() => {
    if (!run) return [];
    if (run.state === "failed" && run.current_step_id) {
      return [run.current_step_id];
    }
    return [];
  }, [run]);

  const currentStep = useMemo(() => {
    if (!run?.current_step_id || !workflow?.steps) return null;
    return workflow.steps.find((s) => s.id === run.current_step_id) || null;
  }, [run, workflow]);

  // Step output viewer state
  const [outputDialogOpen, setOutputDialogOpen] = useState(false);
  const [outputStepId, setOutputStepId] = useState("");
  const [outputStepName, setOutputStepName] = useState("");
  const [outputHistoryEntry, setOutputHistoryEntry] = useState<WorkflowRunHistoryEntry | null>(null);

  const handleNodeDoubleClick = useCallback((_event: React.MouseEvent, node: Node) => {
    if (!run?.history) return;
    const entry = run.history.find((h) => h.step_id === node.id);
    if (!entry) return;
    const step = workflow?.steps?.find((s) => s.id === node.id);
    setOutputStepId(node.id);
    setOutputStepName(step?.name || node.data?.label || node.id);
    setOutputHistoryEntry(entry);
    setOutputDialogOpen(true);
  }, [run, workflow]);

  const handleAdvance = async (action: Record<string, unknown>) => {
    await advanceRun(runId, action);
    await loadState();
  };

  const handleCancel = async () => {
    await cancelRun(runId);
    await loadState();
  };

  const handleExport = async () => {
    if (!run?.history || !workflow) return;
    const steps = await Promise.all(
      run.history.map(async (entry) => {
        const step = workflow.steps?.find((s) => s.id === entry.step_id);
        let output = entry.output_snippet || "";
        try {
          const res = await apiFetch(`/api/workflow-runs/${runId}/output/${entry.step_id}`, { credentials: "same-origin" });
          const data = await res.json();
          if (data.ok && data.output) output = data.output;
        } catch { /* use snippet as fallback */ }
        return { id: entry.step_id, name: step?.name || entry.step_id, output };
      }),
    );
    const payload = {
      workflow: workflow.name,
      run_id: runId,
      state: run.state,
      started_at: run.started_at,
      updated_at: run.updated_at,
      steps,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${workflow.name.replace(/\s+/g, "_")}_${runId.slice(0, 8)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const [viewTab, setViewTab] = useState(0);

  if (loading || !run) {
    return (
      <Box sx={{ p: 3 }}>
        <LinearProgress />
      </Box>
    );
  }

  const isTerminal = ["completed", "failed", "cancelled"].includes(run.state);

  return (
    <Dialog open onClose={onClose} maxWidth="lg" fullWidth>
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 2 }}>
        <Text kind="body/bold/md">{run.workflow_name}</Text>
        <Tag color={STATE_COLORS[run.state] || "gray"} kind="solid" readOnly density="compact">
          {run.state}
        </Tag>
      </DialogTitle>
      <DialogContent sx={{ display: "flex", flexDirection: "column", gap: 0, p: 0, height: 550 }}>
        <Tabs value={viewTab} onChange={(_, v) => setViewTab(v)} sx={{ px: 2, borderBottom: 1, borderColor: "divider", flexShrink: 0 }}>
          <Tab label="Diagram" icon={<GitGraph size={14} />} iconPosition="start" sx={{ textTransform: "none", minHeight: 40 }} />
          <Tab label="Execution Log" icon={<ScrollText size={14} />} iconPosition="start" sx={{ textTransform: "none", minHeight: 40 }} />
        </Tabs>

        {/* Tab 0: Flow diagram */}
        {viewTab === 0 && (
          <>
            <Box sx={{ flex: 1, minHeight: 300 }}>
              {nodes.length > 0 && (
                <FlowDiagram
                  nodes={nodes}
                  edges={edges}
                  activeStepId={run.current_step_id}
                  completedStepIds={completedStepIds}
                  failedStepIds={failedStepIds}
                  onNodeDoubleClick={handleNodeDoubleClick}
                  readOnly
                />
              )}
            </Box>

            {/* Action panel for waiting states */}
            {currentStep && !isTerminal && (
              <Card style={{ margin: "0 24px 16px", padding: 16 }}>
                <Box sx={{ display: "flex", alignItems: "center", gap: 1, mb: 1 }}>
                  {STATE_ICONS[run.state]}
                  <Text kind="body/bold/sm">{currentStep.name}</Text>
                </Box>

                {run.state === "waiting_manual" && (
                  <>
                    {currentStep.instructions && (
                      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 12, display: "block" }}>
                        {currentStep.instructions}
                      </Text>
                    )}
                    <Button
                      variant="contained"
                      size="small"
                      startIcon={<CheckCircle size={16} />}
                      onClick={() => handleAdvance({ completed: true })}
                    >
                      Mark Complete
                    </Button>
                  </>
                )}

                {run.state === "waiting_decision" && (
                  <>
                    {currentStep.question && (
                      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 12, display: "block" }}>
                        {currentStep.question}
                      </Text>
                    )}
                    <Box sx={{ display: "flex", gap: 1, flexWrap: "wrap" }}>
                      {(currentStep.options || []).map((opt) => (
                        <Button
                          key={opt.label}
                          variant={decisionChoice === opt.label ? "contained" : "outlined"}
                          size="small"
                          onClick={() => {
                            setDecisionChoice(opt.label);
                            handleAdvance({ choice: opt.label });
                          }}
                        >
                          {opt.label}
                        </Button>
                      ))}
                    </Box>
                  </>
                )}
              </Card>
            )}
          </>
        )}

        {/* Tab 1: Execution Log */}
        {viewTab === 1 && (
          <Box sx={{ flex: 1, overflow: "auto", p: 2 }}>
            {/* Run metadata */}
            <Box sx={{ mb: 2, display: "flex", gap: 3, flexWrap: "wrap" }}>
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                Started: {run.started_at ? new Date(run.started_at).toLocaleString() : "—"}
              </Text>
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                Updated: {run.updated_at ? new Date(run.updated_at).toLocaleString() : "—"}
              </Text>
              {run.state === "failed" && run.error && (
                <Text kind="body/regular/sm" style={{ color: "var(--color-red-500)" }}>
                  Error: {run.error}
                </Text>
              )}
            </Box>

            {/* Timeline entries */}
            <Box sx={{ display: "flex", flexDirection: "column", gap: 0 }}>
              {(run.history || []).length === 0 && (
                <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                  No execution history yet.
                </Text>
              )}
              {(run.history || []).map((entry, idx: number) => (
                <Box
                  key={idx}
                  sx={{
                    display: "flex",
                    gap: 2,
                    py: 1.5,
                    px: 2,
                    borderLeft: "3px solid",
                    borderColor: entry.success === false ? "error.main" : "success.main",
                    bgcolor: idx % 2 === 0 ? "action.hover" : "transparent",
                    borderRadius: 1,
                    mb: 0.5,
                  }}
                >
                  {/* Status icon */}
                  <Box sx={{ pt: 0.25 }}>
                    {entry.success === false ? (
                      <XCircle size={16} color="#ef4444" />
                    ) : (
                      <CheckCircle size={16} color="#22c55e" />
                    )}
                  </Box>

                  {/* Content */}
                  <Box sx={{ flex: 1, minWidth: 0 }}>
                    <Box sx={{ display: "flex", alignItems: "center", gap: 1, mb: 0.5 }}>
                      <Text kind="body/bold/sm">{entry.step_id}</Text>
                      <Tag
                        color={entry.success === false ? "red" : "green"}
                        kind="outline"
                        readOnly
                        density="compact"
                      >
                        {entry.type || "step"}
                      </Tag>
                      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginLeft: "auto" }}>
                        {entry.completed_at ? new Date(entry.completed_at).toLocaleTimeString() : ""}
                      </Text>
                    </Box>

                    {/* Evaluation message (why it passed/failed) */}
                    {entry.message && (
                      <Text
                        kind="body/regular/sm"
                        style={{
                          display: "block",
                          marginBottom: 4,
                          color: entry.success === false ? "var(--text-color-danger, #f87171)" : "var(--text-color-secondary)",
                        }}
                      >
                        {entry.message}
                      </Text>
                    )}
                    {/* Agent triage hint */}
                    {entry.prompt && entry.success === false && (
                      <Text
                        kind="body/regular/sm"
                        style={{ display: "block", marginBottom: 4, color: "var(--text-color-secondary)", fontStyle: "italic" }}
                      >
                        Hint: {entry.prompt}
                      </Text>
                    )}

                    {/* Output snippet */}
                    {entry.output_snippet && (
                      <Box
                        onClick={() => {
                          const step = workflow?.steps?.find((s) => s.id === entry.step_id);
                          setOutputStepId(entry.step_id);
                          setOutputStepName(step?.name || entry.step_id);
                          setOutputHistoryEntry(entry);
                          setOutputDialogOpen(true);
                        }}
                        sx={{
                          mt: 0.5,
                          p: 1,
                          bgcolor: "#0d1117",
                          borderRadius: 0.5,
                          fontFamily: "ui-monospace, monospace",
                          fontSize: 12,
                          lineHeight: 1.4,
                          color: "#8b949e",
                          whiteSpace: "pre-wrap",
                          wordBreak: "break-word",
                          maxHeight: 400,
                          overflow: "auto",
                          cursor: "pointer",
                          "&:hover": { borderColor: "primary.main", border: "1px solid" },
                        }}
                      >
                        {entry.output_snippet}
                      </Box>
                    )}
                    {entry.output_file && (
                      <Text
                        kind="body/regular/sm"
                        style={{ color: "var(--nv-color-brand-500, #76b900)", cursor: "pointer", marginTop: 4, display: "inline-block" }}
                        onClick={() => {
                          const step = workflow?.steps?.find((s) => s.id === entry.step_id);
                          setOutputStepId(entry.step_id);
                          setOutputStepName(step?.name || entry.step_id);
                          setOutputHistoryEntry(entry);
                          setOutputDialogOpen(true);
                        }}
                      >
                        View full output →
                      </Text>
                    )}
                  </Box>
                </Box>
              ))}

              {/* Current state indicator */}
              {!isTerminal && run.current_step_id && (
                <Box
                  sx={{
                    display: "flex",
                    gap: 2,
                    py: 1.5,
                    px: 2,
                    borderLeft: "3px solid",
                    borderColor: "info.main",
                    borderRadius: 1,
                    animation: "pulse 2s infinite",
                    "@keyframes pulse": { "0%,100%": { opacity: 1 }, "50%": { opacity: 0.6 } },
                  }}
                >
                  <Box sx={{ pt: 0.25 }}>
                    <Clock size={16} color="#60a5fa" />
                  </Box>
                  <Box>
                    <Text kind="body/bold/sm">{run.current_step_id}</Text>
                    <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                      {run.state === "waiting_manual" ? "Waiting for manual confirmation…" :
                       run.state === "waiting_decision" ? "Waiting for decision…" :
                       "Running…"}
                    </Text>
                  </Box>
                </Box>
              )}

              {/* Terminal state */}
              {isTerminal && (
                <Box
                  sx={{
                    display: "flex",
                    gap: 2,
                    py: 1.5,
                    px: 2,
                    borderLeft: "3px solid",
                    borderColor: run.state === "completed" ? "success.main" : run.state === "failed" ? "error.main" : "grey.500",
                    borderRadius: 1,
                    mt: 1,
                  }}
                >
                  <Box sx={{ pt: 0.25 }}>
                    {run.state === "completed" ? <CheckCircle size={16} color="#22c55e" /> :
                     run.state === "failed" ? <XCircle size={16} color="#ef4444" /> :
                     <AlertTriangle size={16} color="#6b7280" />}
                  </Box>
                  <Box>
                    <Text kind="body/bold/sm">
                      {run.state === "completed" ? "Workflow completed successfully" :
                       run.state === "failed" ? "Workflow failed" :
                       "Workflow cancelled"}
                    </Text>
                    <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                      {run.updated_at ? new Date(run.updated_at).toLocaleString() : ""}
                    </Text>
                  </Box>
                </Box>
              )}
            </Box>
          </Box>
        )}
      </DialogContent>
      <DialogActions>
        {!isTerminal && (
          <MuiButton variant="outlined" color="error" onClick={handleCancel} startIcon={<XCircle size={16} />}>
            Cancel Run
          </MuiButton>
        )}
        <MuiButton variant="outlined" onClick={handleExport} startIcon={<Download size={16} />}>
          Export
        </MuiButton>
        <MuiButton variant="outlined" onClick={onClose}>Close</MuiButton>
      </DialogActions>

      {/* Keyed by step so opening a different step remounts the dialog and its tab/output state
          resets on its own. The alternative was resetting that state from inside an effect,
          which costs an extra render pass and is what React's own guidance steers away from. */}
      <StepOutputDialog
        key={outputStepId}
        open={outputDialogOpen}
        onOpenChange={setOutputDialogOpen}
        stepId={outputStepId}
        stepName={outputStepName}
        runId={runId}
        historyEntry={outputHistoryEntry}
      />
    </Dialog>
  );
}
