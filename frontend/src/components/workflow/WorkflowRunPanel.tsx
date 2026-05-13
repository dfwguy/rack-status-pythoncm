import { useState, useEffect, useCallback, useMemo } from "react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import MuiButton from "@mui/material/Button";
import LinearProgress from "@mui/material/LinearProgress";
import { Text, Tag, Card } from "@nvidia/foundations-react-core";
import { Play, XCircle, CheckCircle, Clock, AlertTriangle } from "lucide-react";
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
  type Workflow,
} from "../../services/workflowService";

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
  const [outputHistoryEntry, setOutputHistoryEntry] = useState<any>(null);

  const handleNodeDoubleClick = useCallback((_event: React.MouseEvent, node: Node) => {
    if (!run?.history) return;
    const entry = run.history.find((h: any) => h.step_id === node.id);
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
      <DialogContent sx={{ display: "flex", flexDirection: "column", gap: 2, p: 0, height: 500 }}>
        {/* Flow diagram */}
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
      </DialogContent>
      <DialogActions>
        {!isTerminal && (
          <MuiButton color="error" onClick={handleCancel} startIcon={<XCircle size={16} />}>
            Cancel Run
          </MuiButton>
        )}
        <MuiButton onClick={onClose}>Close</MuiButton>
      </DialogActions>

      <StepOutputDialog
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
