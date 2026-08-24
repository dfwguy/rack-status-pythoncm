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
import { useCallback, useEffect, useMemo, useState } from "react";
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
import { Banner, Button, Card, Flex, Tag, Text } from "@nvidia/foundations-react-core";
import { ClipboardList, Play, RefreshCw, type LucideIcon } from "lucide-react";
import { validationTargetFromNodes } from "../../lib/validationTargetFromNodes";
import { fetchDeepAuditOutput, fetchDeepAudits, startDeepAudit } from "../../services/nodeIssuesService";
import type { DeepAuditRun, DeepAuditSummary } from "../../types/api";
import type { NvisNamingSchema } from "../../types/naming";

type AuditKind = "hardware" | "network";
type WorkflowId = "hardware-deep-audit" | "nvlink-network-deep-audit";

interface DeepAuditCardProps {
  rack: string;
  selectedNodes: Set<string>;
  naming?: NvisNamingSchema;
  disabled?: boolean;
  kind: AuditKind;
  title: string;
  description: string;
  icon: LucideIcon;
}

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

function fmtWhen(value: string): string {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

function stateColor(state: string): "green" | "red" | "yellow" | "blue" | "gray" {
  if (state === "completed") return "green";
  if (state === "failed" || state === "cancelled") return "red";
  if (state === "running") return "yellow";
  if (state === "pending") return "blue";
  return "gray";
}

function resultColor(status: string): "green" | "red" | "yellow" | "gray" {
  if (status === "pass") return "green";
  if (status === "fail") return "red";
  if (status === "warning") return "yellow";
  return "gray";
}

function summaryOf(run?: DeepAuditRun): DeepAuditSummary | null {
  if (!run) return null;
  return Object.values(run.audits || {})[0] ?? null;
}

export function DeepAuditCard({
  rack,
  selectedNodes,
  naming,
  disabled,
  kind,
  title,
  description,
  icon: Icon,
}: DeepAuditCardProps) {
  const workflowId: WorkflowId = kind === "hardware" ? "hardware-deep-audit" : "nvlink-network-deep-audit";
  const [runs, setRuns] = useState<DeepAuditRun[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [resultsOpen, setResultsOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [outputOpen, setOutputOpen] = useState(false);
  const [outputText, setOutputText] = useState("");
  const [outputLoading, setOutputLoading] = useState(false);

  const latest = runs[0];
  const latestSummary = summaryOf(latest);
  const active = runs.some((run) => !TERMINAL.has(run.state));

  const refresh = useCallback(async () => {
    try {
      setRuns(await fetchDeepAudits({ rack, kind, limit: 20 }));
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [rack, kind]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!active && !resultsOpen) return;
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(timer);
  }, [active, resultsOpen, refresh]);

  const target = useMemo(
    () => validationTargetFromNodes(rack, selectedNodes, naming) || "",
    [rack, selectedNodes, naming],
  );

  const launch = async () => {
    setConfirmOpen(false);
    if (!target) return;
    setLoading(true);
    setError("");
    try {
      await startDeepAudit({ workflowId, rack, target });
      setResultsOpen(true);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  const openOutput = async (run: DeepAuditRun) => {
    setOutputText("");
    setOutputLoading(true);
    setOutputOpen(true);
    try {
      setOutputText(await fetchDeepAuditOutput(run.run_id));
    } catch (e) {
      setOutputText(e instanceof Error ? e.message : String(e));
    } finally {
      setOutputLoading(false);
    }
  };

  return (
    <>
      <Card style={{ padding: 20 }}>
        <Flex gap="3" align="start">
          <div style={{ color: "var(--text-color-secondary)", marginTop: 2 }}>
            <Icon size={20} />
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <Flex gap="2" align="center" wrap="wrap" style={{ marginBottom: 4 }}>
              <Text kind="body/bold/md">{title}</Text>
              {latest && (
                <Tag color={stateColor(latest.state)} kind="outline" readOnly density="compact">
                  {latest.state}
                </Tag>
              )}
              {latestSummary && latestSummary.failed > 0 && (
                <Tag color="red" kind="solid" readOnly density="compact">
                  {latestSummary.failed} failed · {latestSummary.drained} drained
                </Tag>
              )}
            </Flex>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              {description}
            </Text>
            {error && (
              <Text kind="body/regular/sm" style={{ display: "block", color: "#d32f2f", marginTop: 6 }}>
                {error}
              </Text>
            )}
          </div>
          <Flex gap="2" align="center">
            <Button kind="secondary" size="small" onClick={() => setResultsOpen(true)}>
              <ClipboardList size={15} />
              Results
            </Button>
            <Button
              kind="primary"
              size="small"
              disabled={disabled || loading || active || !target}
              onClick={() => setConfirmOpen(true)}
            >
              <Play size={15} />
              {loading || active ? "Running…" : "Start Tests"}
            </Button>
          </Flex>
        </Flex>
      </Card>

      <Dialog open={confirmOpen} onClose={() => setConfirmOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>{title}</DialogTitle>
        <DialogContent>
          <Banner status="warning" kind="inline">
            Confirmed node failures will be tagged in issue history and immediately drained through the active scheduler with this
            validation ID and the exact reason. Warnings and unavailable probes remain inconclusive and are not drained.
          </Banner>
          <Text kind="body/regular/md" style={{ display: "block", marginTop: 16 }}>
            Run on {selectedNodes.size} selected node(s) in {rack}?
          </Text>
        </DialogContent>
        <DialogActions>
          <Button kind="secondary" onClick={() => setConfirmOpen(false)}>Cancel</Button>
          <Button kind="primary" onClick={() => void launch()}>Start Tests</Button>
        </DialogActions>
      </Dialog>

      <Dialog open={resultsOpen && !outputOpen} onClose={() => setResultsOpen(false)} maxWidth="xl" fullWidth>
        <DialogTitle>
          <Flex align="center" gap="2">
            <span style={{ flex: 1 }}>{title} results — {rack}</span>
            <Button kind="secondary" size="small" onClick={() => void refresh()}>
              <RefreshCw size={14} /> Refresh
            </Button>
          </Flex>
        </DialogTitle>
        <DialogContent>
          {error && <Banner status="error" kind="inline">{error}</Banner>}
          {!latest ? (
            <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
              No {kind} deep audit has run for this rack yet.
            </Text>
          ) : (
            <>
              <Flex gap="2" wrap="wrap" style={{ marginBottom: 14 }}>
                <Tag color={stateColor(latest.state)} kind="solid" readOnly>{latest.state}</Tag>
                <Tag color="gray" kind="outline" readOnly>{latest.run_id.slice(0, 8)}</Tag>
                <Tag color="gray" kind="outline" readOnly>
                  Job {latestSummary?.nodes.map((node) => node.scheduler_ref).find(Boolean) || "—"}
                </Tag>
                <Text kind="body/regular/sm">Started {fmtWhen(latest.started_at)}</Text>
                {latestSummary && (
                  <Text kind="body/regular/sm">
                    Pass {latestSummary.passed} · Warning {latestSummary.warning} · Fail {latestSummary.failed} ·
                    Inconclusive {latestSummary.inconclusive} · Drained {latestSummary.drained}
                  </Text>
                )}
                <Button kind="secondary" size="small" onClick={() => void openOutput(latest)}>Full output</Button>
              </Flex>
              {latestSummary?.drain_failed ? (
                <Banner status="error" kind="inline" style={{ marginBottom: 12 }}>
                  {latestSummary.drain_failed} confirmed failure(s) could not be drained. Review the per-node scheduler result.
                </Banner>
              ) : null}
              {latestSummary ? (
                <TableContainer component={Paper} variant="outlined">
                  <Table size="small" stickyHeader>
                    <TableHead>
                      <TableRow>
                        <TableCell>Node</TableCell>
                        <TableCell>Job ID</TableCell>
                        <TableCell>Result</TableCell>
                        <TableCell>Finding / warning</TableCell>
                        <TableCell>Scheduler action</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {latestSummary.nodes.map((node) => (
                        <TableRow key={node.node} hover>
                          <TableCell sx={{ fontFamily: "monospace" }}>{node.node}</TableCell>
                          <TableCell sx={{ fontFamily: "monospace" }}>{node.scheduler_ref || "—"}</TableCell>
                          <TableCell>
                            <Tag color={resultColor(node.status)} kind="solid" readOnly density="compact">
                              {node.status}
                            </Tag>
                          </TableCell>
                          <TableCell sx={{ maxWidth: 560, whiteSpace: "normal" }}>
                            {node.reason || node.warnings?.join("; ") || "Healthy"}
                            {node.codes?.length ? (
                              <Text kind="mono/sm" style={{ display: "block", color: "var(--text-color-secondary)" }}>
                                {node.codes.join(", ")}
                              </Text>
                            ) : null}
                          </TableCell>
                          <TableCell sx={{ maxWidth: 420, whiteSpace: "normal" }}>
                            {node.drained ? (
                              <Tag color="red" kind="outline" readOnly density="compact">drained</Tag>
                            ) : node.status === "fail" ? (
                              <Tag color="red" kind="solid" readOnly density="compact">drain failed</Tag>
                            ) : (
                              "—"
                            )}
                            {node.slurm_reason ? (
                              <Text kind="body/regular/sm" style={{ display: "block", marginTop: 4 }}>
                                {node.slurm_reason}
                              </Text>
                            ) : null}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </TableContainer>
              ) : (
                <Banner status="info" kind="inline">The fan-out is still running; results will appear as it finishes.</Banner>
              )}
            </>
          )}
        </DialogContent>
        <DialogActions>
          <Button kind="secondary" onClick={() => setResultsOpen(false)}>Close</Button>
        </DialogActions>
      </Dialog>

      <Dialog open={outputOpen} onClose={() => setOutputOpen(false)} maxWidth="xl" fullWidth>
        <DialogTitle>{title} full output — {latest?.run_id.slice(0, 8) || ""}</DialogTitle>
        <DialogContent dividers>
          <pre style={{ margin: 0, padding: 16, background: "#101010", color: "#f2f2f2", maxHeight: "70vh", overflow: "auto", whiteSpace: "pre-wrap" }}>
            {outputLoading ? "Loading full output…" : outputText || "No output was recorded."}
          </pre>
        </DialogContent>
        <DialogActions><Button kind="secondary" onClick={() => setOutputOpen(false)}>Close</Button></DialogActions>
      </Dialog>
    </>
  );
}
