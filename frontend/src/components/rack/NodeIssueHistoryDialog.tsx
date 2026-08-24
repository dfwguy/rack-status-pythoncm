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
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import { Banner, Button, Flex, Tag, Text } from "@nvidia/foundations-react-core";
import type { NodeIssue, NodeIssueRollup } from "../../types/api";

interface NodeIssueHistoryDialogProps {
  open: boolean;
  node: string;
  rollup?: NodeIssueRollup;
  issues: NodeIssue[];
  punchcardSyncedAt?: string;
  onClose: () => void;
}

function when(value: string): string {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

export function NodeIssueHistoryDialog({
  open,
  node,
  rollup,
  issues,
  punchcardSyncedAt,
  onClose,
}: NodeIssueHistoryDialogProps) {
  const local = issues.filter((issue) => issue.node === node);
  return (
    <Dialog open={open} onClose={onClose} maxWidth="lg" fullWidth>
      <DialogTitle>Node issue history — {node}</DialogTitle>
      <DialogContent>
        <Flex gap="2" wrap="wrap" style={{ marginBottom: 14 }}>
          <Tag color={(rollup?.issue_count || 0) > 1 ? "yellow" : "gray"} kind="solid" readOnly>
            ▲{rollup?.issue_count ?? 0} total
          </Tag>
          {rollup?.drained && <Tag color="red" kind="solid" readOnly>✖ currently drained</Tag>}
          {rollup?.nccl && <Tag color="purple" kind="outline" readOnly>⚑ network/NCCL</Tag>}
          {rollup?.maintenance && <Tag color="blue" kind="outline" readOnly>▦ maintenance</Tag>}
        </Flex>

        {(rollup?.imported_count || 0) > 0 && (
          <Banner status="info" kind="inline" style={{ marginBottom: 14 }}>
            Imported punchcard baseline: {rollup?.imported_count} prior issue/comment event(s)
            {punchcardSyncedAt ? ` (synced ${when(punchcardSyncedAt)})` : ""}. Google exposes the count and flags,
            but not the underlying cell-comment text; detailed history below is retained for Rack Status findings from now on.
          </Banner>
        )}

        {local.length === 0 ? (
          <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
            No detailed Rack Status findings have been recorded for this node yet.
          </Text>
        ) : (
          <TableContainer component={Paper} variant="outlined">
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  <TableCell>When</TableCell>
                  <TableCell>Area / code</TableCell>
                  <TableCell>Finding</TableCell>
                  <TableCell>Validation</TableCell>
                  <TableCell>Scheduler action</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {local.map((issue) => (
                  <TableRow key={issue.issue_id} hover>
                    <TableCell>{when(issue.created_at)}</TableCell>
                    <TableCell>
                      <Tag color={issue.category === "network" ? "purple" : "blue"} kind="outline" readOnly density="compact">
                        {issue.category}
                      </Tag>
                      <Text kind="mono/sm" style={{ display: "block", marginTop: 3 }}>{issue.code}</Text>
                    </TableCell>
                    <TableCell sx={{ maxWidth: 420, whiteSpace: "normal" }}>{issue.reason}</TableCell>
                    <TableCell>
                      <Text kind="mono/sm">{issue.validation_run_id?.slice(0, 8) || "—"}</Text>
                      <Text kind="body/regular/sm" style={{ display: "block", color: "var(--text-color-secondary)" }}>
                        {issue.source}
                      </Text>
                    </TableCell>
                    <TableCell sx={{ maxWidth: 360, whiteSpace: "normal" }}>
                      <Tag color={issue.drained ? "red" : "gray"} kind="solid" readOnly density="compact">
                        {issue.drained ? "drained" : issue.slurm_reason ? "scheduler comment" : "recorded"}
                      </Tag>
                      {issue.slurm_reason && (
                        <Text kind="body/regular/sm" style={{ display: "block", marginTop: 4 }}>
                          {issue.slurm_reason}
                        </Text>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        )}
      </DialogContent>
      <DialogActions>
        <Button kind="secondary" onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}
