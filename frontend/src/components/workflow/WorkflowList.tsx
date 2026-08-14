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
import { useState, useEffect, useCallback } from "react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Paper from "@mui/material/Paper";
import Tabs from "@mui/material/Tabs";
import Tab from "@mui/material/Tab";
import { Text, Tag, Modal, ModalCloseButton, Button as KuiButton } from "@nvidia/foundations-react-core";
import { Plus, Play, Trash2, Edit, RefreshCw } from "lucide-react";
import { usePagedRows } from "../../hooks/usePagedRows";
import { TablePager } from "../../components/TablePager";
import {
  listWorkflows,
  listRuns,
  deleteWorkflow,
  type WorkflowSummary,
  type WorkflowRunSummary,
} from "../../services/workflowService";

interface WorkflowListProps {
  onSelect: (workflowId: string) => void;
  onNew: () => void;
  onViewRun: (runId: string) => void;
  onRun: (workflowId: string) => void;
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

export default function WorkflowList({ onSelect, onNew, onViewRun, onRun }: WorkflowListProps) {
  const [tab, setTab] = useState(0);
  const [workflows, setWorkflows] = useState<WorkflowSummary[]>([]);
  // Saved workflows and their run history both accumulate without bound.
  const wfPage = usePagedRows(workflows, 10);
  const [runs, setRuns] = useState<WorkflowRunSummary[]>([]);
  const runsPage = usePagedRows(runs, 10);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [wfs, rns] = await Promise.all([listWorkflows(), listRuns()]);
      setWorkflows(wfs);
      setRuns(rns);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  const handleDeleteConfirm = async () => {
    if (!deleteTarget) return;
    await deleteWorkflow(deleteTarget);
    setDeleteTarget(null);
    refresh();
  };

  const handleRun = (id: string) => {
    onRun(id);
  };

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <Box sx={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <Tabs value={tab} onChange={(_, v) => setTab(v)}>
          <Tab label="Templates" />
          <Tab label="Run History" />
        </Tabs>
        <Box sx={{ display: "flex", gap: 1 }}>
          <Button variant="outlined" size="small" startIcon={<RefreshCw size={14} />} onClick={() => refresh()} disabled={loading}>
            Refresh
          </Button>
          <Button variant="contained" size="small" startIcon={<Plus size={16} />} onClick={onNew}>
            New Workflow
          </Button>
        </Box>
      </Box>

      {tab === 0 && (
        <>
        <TableContainer component={Paper} variant="outlined">
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Name</TableCell>
                <TableCell>Description</TableCell>
                <TableCell align="center">Steps</TableCell>
                <TableCell align="center">Version</TableCell>
                <TableCell align="right">Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {workflows.length === 0 && !loading && (
                <TableRow>
                  <TableCell colSpan={5} align="center">
                    <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", padding: "24px 0" }}>
                      No workflows yet. Create one to get started.
                    </Text>
                  </TableCell>
                </TableRow>
              )}
              {wfPage.visible.map((wf) => (
                <TableRow key={wf.id} hover sx={{ cursor: "pointer" }} onClick={() => onSelect(wf.id)}>
                  <TableCell>
                    <Text kind="body/bold/sm">{wf.name}</Text>
                  </TableCell>
                  <TableCell>
                    <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                      {wf.description || "—"}
                    </Text>
                  </TableCell>
                  <TableCell align="center">{wf.step_count}</TableCell>
                  <TableCell align="center">v{wf.version}</TableCell>
                  <TableCell align="right" onClick={(e) => e.stopPropagation()}>
                    <Tooltip title="Edit">
                      <IconButton size="small" onClick={() => onSelect(wf.id)}>
                        <Edit size={16} />
                      </IconButton>
                    </Tooltip>
                    <Tooltip title="Run">
                      <IconButton size="small" onClick={() => handleRun(wf.id)}>
                        <Play size={16} />
                      </IconButton>
                    </Tooltip>
                    <Tooltip title="Delete">
                      <IconButton size="small" onClick={() => setDeleteTarget(wf.id)}>
                        <Trash2 size={16} />
                      </IconButton>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
          <TablePager {...wfPage} noun="workflows" />
        </>
      )}

      {tab === 1 && (
        <>
        <TableContainer component={Paper} variant="outlined">
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>Workflow</TableCell>
                <TableCell>Run ID</TableCell>
                <TableCell>State</TableCell>
                <TableCell>Current Step</TableCell>
                <TableCell>Started</TableCell>
                <TableCell>Updated</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {runs.length === 0 && !loading && (
                <TableRow>
                  <TableCell colSpan={6} align="center">
                    <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", padding: "24px 0" }}>
                      No workflow runs yet.
                    </Text>
                  </TableCell>
                </TableRow>
              )}
              {runsPage.visible.map((run) => (
                <TableRow
                  key={run.run_id}
                  hover
                  sx={{ cursor: "pointer" }}
                  onClick={() => onViewRun(run.run_id)}
                >
                  <TableCell>
                    <Text kind="body/regular/sm">{run.workflow_name}</Text>
                  </TableCell>
                  <TableCell>
                    <Text kind="mono/sm" style={{ color: "var(--text-color-secondary)" }}>
                      {run.run_id.slice(0, 8)}
                    </Text>
                  </TableCell>
                  <TableCell>
                    <Tag color={STATE_COLORS[run.state] || "gray"} kind="solid" readOnly density="compact">
                      {run.state}
                    </Tag>
                  </TableCell>
                  <TableCell>
                    <Text kind="mono/sm" style={{ color: "var(--text-color-secondary)" }}>
                      {run.current_step_id || "—"}
                    </Text>
                  </TableCell>
                  <TableCell>
                    <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                      {run.started_at ? new Date(run.started_at).toLocaleString() : "—"}
                    </Text>
                  </TableCell>
                  <TableCell>
                    <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                      {run.updated_at ? new Date(run.updated_at).toLocaleString() : "—"}
                    </Text>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
          <TablePager {...runsPage} noun="runs" />
        </>
      )}

      <Modal
        open={!!deleteTarget}
        onOpenChange={(open) => { if (!open) setDeleteTarget(null); }}
        modal={true}
        slotHeading="Delete Workflow"
        slotFooter={
          <>
            <ModalCloseButton kind="secondary">Cancel</ModalCloseButton>
            <KuiButton kind="primary" color="danger" onClick={handleDeleteConfirm}>
              Delete
            </KuiButton>
          </>
        }
      >
        <Text kind="label/regular/xl">
          Are you sure you want to delete this workflow? This action cannot be undone.
        </Text>
        <Text kind="label/regular/xl" style={{ marginTop: 16, display: "block" }}>
          ID: {deleteTarget}
        </Text>
        <Text kind="label/regular/xl" style={{ color: "var(--text-color-secondary)" }}>
          Name: {workflows.find((w) => w.id === deleteTarget)?.name || deleteTarget}
        </Text>
      </Modal>
    </Box>
  );
}
