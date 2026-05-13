import { useState, useCallback } from "react";
import Box from "@mui/material/Box";
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import { ArrowLeft } from "lucide-react";
import { PageHeader } from "../components/layout/PageHeader";
import WorkflowList from "../components/workflow/WorkflowList";
import WorkflowDesigner from "../components/workflow/WorkflowDesigner";
import WorkflowRunPanel from "../components/workflow/WorkflowRunPanel";
import { getWorkflow, saveWorkflow, type Workflow } from "../services/workflowService";

type ViewMode = "list" | "designer";

export function WorkflowsView() {
  const [mode, setMode] = useState<ViewMode>("list");
  const [selectedWorkflow, setSelectedWorkflow] = useState<Workflow | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);

  const handleSelect = useCallback(async (workflowId: string) => {
    const wf = await getWorkflow(workflowId);
    setSelectedWorkflow(wf);
    setMode("designer");
  }, []);

  const handleNew = useCallback(() => {
    setSelectedWorkflow(null);
    setMode("designer");
  }, []);

  const handleBack = useCallback(() => {
    setMode("list");
    setSelectedWorkflow(null);
  }, []);

  const handleSave = useCallback(async (workflow: Partial<Workflow>) => {
    const saved = await saveWorkflow(workflow as Workflow);
    setSelectedWorkflow(saved);
  }, []);

  const handleRun = useCallback((_workflowId: string) => {
    // startWorkflowRun is handled in WorkflowList, which calls onViewRun
  }, []);

  const handleViewRun = useCallback((runId: string) => {
    setActiveRunId(runId);
  }, []);

  return (
    <Box sx={{ height: "calc(100vh - 64px)", display: "flex", flexDirection: "column" }}>
      {mode === "list" && (
        <Box sx={{ p: 3, flex: 1, overflow: "auto" }}>
          <PageHeader
            title="Workflows"
            description="Create and manage rack troubleshooting and validation workflows. Define automated steps, manual checkpoints, and decision points."
          />
          <WorkflowList onSelect={handleSelect} onNew={handleNew} onViewRun={handleViewRun} />
        </Box>
      )}

      {mode === "designer" && (
        <Box sx={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden", minHeight: 0 }}>
          <Box sx={{ display: "flex", alignItems: "center", gap: 1, px: 2, py: 1, borderBottom: "1px solid", borderColor: "divider", flexShrink: 0 }}>
            <Tooltip title="Back to list">
              <IconButton size="small" onClick={handleBack}>
                <ArrowLeft size={20} />
              </IconButton>
            </Tooltip>
            <PageHeader
              title={selectedWorkflow ? `Edit: ${selectedWorkflow.name}` : "New Workflow"}
              description=""
            />
          </Box>
          <Box sx={{ flex: 1, overflow: "hidden", minHeight: 0 }}>
            <WorkflowDesigner
              workflow={selectedWorkflow}
              onSave={handleSave}
              onRun={handleRun}
            />
          </Box>
        </Box>
      )}

      {activeRunId && (
        <WorkflowRunPanel runId={activeRunId} onClose={() => setActiveRunId(null)} />
      )}
    </Box>
  );
}
