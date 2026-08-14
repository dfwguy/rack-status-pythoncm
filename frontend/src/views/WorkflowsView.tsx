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
import { useState, useCallback, useMemo } from "react";
import Box from "@mui/material/Box";
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import { ArrowLeft } from "lucide-react";
import { PageHeader } from "../components/layout/PageHeader";
import WorkflowList from "../components/workflow/WorkflowList";
import WorkflowDesigner from "../components/workflow/WorkflowDesigner";
import WorkflowRunPanel from "../components/workflow/WorkflowRunPanel";
import WorkflowRunDialog from "../components/workflow/WorkflowRunDialog";
import {
  getWorkflow,
  saveWorkflow,
  startWorkflowRun,
  type Workflow,
  type WorkflowTarget,
} from "../services/workflowService";
import type { RackStatusResponse } from "../types/api";

type ViewMode = "list" | "designer";

interface WorkflowsViewProps {
  rackData?: RackStatusResponse | null;
}

export function WorkflowsView({ rackData = null }: WorkflowsViewProps) {
  const [mode, setMode] = useState<ViewMode>("list");
  const [selectedWorkflow, setSelectedWorkflow] = useState<Workflow | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);

  const [runDialogOpen, setRunDialogOpen] = useState(false);
  const [runDialogWorkflowId, setRunDialogWorkflowId] = useState("");
  const [runDialogWorkflowName, setRunDialogWorkflowName] = useState("");
  const [runDialogWorkflowDesc, setRunDialogWorkflowDesc] = useState("");
  const [runDialogTarget, setRunDialogTarget] = useState<WorkflowTarget | undefined>();

  const availableRacks = useMemo(
    () => (rackData?.rows || []).map((r) => r.rack).sort(),
    [rackData],
  );

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

  const openRunDialog = useCallback(async (workflowId: string) => {
    try {
      const wf = await getWorkflow(workflowId);
      setRunDialogWorkflowId(workflowId);
      setRunDialogWorkflowName(wf.name);
      setRunDialogWorkflowDesc(wf.description || "");
      setRunDialogTarget(wf.target);
      setRunDialogOpen(true);
    } catch {
      const result = await startWorkflowRun(workflowId);
      setActiveRunId(result.run_id);
    }
  }, []);

  const handleRunConfirm = useCallback(async (params: Record<string, string>) => {
    const result = await startWorkflowRun(runDialogWorkflowId, params);
    setActiveRunId(result.run_id);
  }, [runDialogWorkflowId]);

  const handleViewRun = useCallback((runId: string) => {
    setActiveRunId(runId);
  }, []);

  return (
    <Box sx={{ height: "calc(100vh - 64px)", display: "flex", flexDirection: "column" }}>
      {mode === "list" && (
        <Box sx={{ p: 3, flex: 1, overflow: "auto" }}>
          <PageHeader
            contentId="workflows"
            title="Workflows"
            description="Create and manage rack troubleshooting and validation workflows. Define automated steps, manual checkpoints, and decision points."
          />
          <WorkflowList onSelect={handleSelect} onNew={handleNew} onViewRun={handleViewRun} onRun={openRunDialog} />
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
              contentId="workflowDesigner"
              title={selectedWorkflow ? `Edit: ${selectedWorkflow.name}` : "New Workflow"}
              description=""
            />
          </Box>
          <Box sx={{ flex: 1, overflow: "hidden", minHeight: 0 }}>
            <WorkflowDesigner
              workflow={selectedWorkflow}
              onSave={handleSave}
              onRun={openRunDialog}
              availableRacks={availableRacks}
            />
          </Box>
        </Box>
      )}

      {activeRunId && (
        <WorkflowRunPanel runId={activeRunId} onClose={() => setActiveRunId(null)} />
      )}

      <WorkflowRunDialog
        open={runDialogOpen}
        onClose={() => setRunDialogOpen(false)}
        onRun={handleRunConfirm}
        workflowName={runDialogWorkflowName}
        workflowDescription={runDialogWorkflowDesc}
        defaultTarget={runDialogTarget}
        rackData={rackData}
      />
    </Box>
  );
}
