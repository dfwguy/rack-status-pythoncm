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
import Box from "@mui/material/Box";
import Checkbox from "@mui/material/Checkbox";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import FormControlLabel from "@mui/material/FormControlLabel";
import MenuItem from "@mui/material/MenuItem";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import { Banner, Button, Card, Checkbox as KuiCheckbox, Flex, Tag, Text } from "@nvidia/foundations-react-core";
import {
  Activity,
  Flag,
  Minus,
  Network,
  Play,
  RefreshCw,
  ShieldCheck,
  TrendingDown,
  TrendingUp,
  Wrench,
} from "lucide-react";
import { PageHeader } from "../components/layout/PageHeader";
import { useConfirm } from "../components/useConfirm";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { TablePager } from "../components/TablePager";
import { usePagedRows } from "../hooks/usePagedRows";
import {
  fetchDeepAudit,
  fetchDeepAuditOutput,
  fetchPostRunNodeReport,
  markPostRunNode,
  startDeepAudit,
} from "../services/nodeIssuesService";
import type { PostRunAuditStatus, PostRunNode, PostRunNodeReportResponse } from "../types/api";

type Section = "history" | "hardware" | "network";
type ActiveAudit = { runId: string; kind: "hardware" | "network"; node: string };

const TERMINAL_AUDIT_STATES = new Set(["completed", "failed", "cancelled"]);

const CATEGORY_OPTIONS = [
  "validation",
  "hardware",
  "network",
  "gpu",
  "thermal",
  "storage",
  "control_plane",
  "maintenance",
];

function formatWhen(value: string): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function stateColor(state: string | null | undefined): "green" | "red" | "yellow" | "blue" | "gray" {
  // Defensive: a run merged from an external evidence feed may omit `state`; never assume
  // the field is a string (the API type says so, the data does not always agree).
  const low = (state ?? "").toLowerCase();
  if (low === "idle" || low === "completed" || low === "pass") return "green";
  if (low.includes("drain") || low === "down" || low === "failed" || low === "fail") return "red";
  if (low === "running" || low === "alloc" || low === "warning") return "yellow";
  if (low === "inconclusive") return "blue";
  return "gray";
}

function TrendIcon({ trend }: { trend: PostRunNode["trend"] }) {
  if (trend === "up" || trend === "new") return <TrendingUp size={15} color="#d32f2f" aria-label="failure trend increasing" />;
  if (trend === "down") return <TrendingDown size={15} color="#2e7d32" aria-label="failure trend decreasing" />;
  return <Minus size={15} color="var(--text-color-secondary)" aria-label="failure trend flat" />;
}

function punchColor(node: PostRunNode): string {
  if (node.flagged) return "rgba(211,47,47,.22)";
  if (node.failure_count > 0) return "rgba(237,108,2,.18)";
  if (node.participation_count > 0) return "rgba(46,125,50,.14)";
  return "var(--surface-color-raised, rgba(127,127,127,.08))";
}

function AuditCell({ audit, onOutput }: { audit: PostRunAuditStatus | null; onOutput?: (audit: PostRunAuditStatus) => void }) {
  if (!audit) return <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>Not run</Text>;
  return (
    <Flex direction="column" gap="1">
      <Tag color={stateColor(audit.status)} kind="solid" readOnly density="compact">{audit.status}</Tag>
      <Text kind="body/regular/sm">{audit.reason || audit.warnings?.join("; ") || "Healthy"}</Text>
      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
        Job {audit.scheduler_ref || "—"} · {audit.run_id.slice(0, 8)} · {formatWhen(audit.started_at)}
      </Text>
      {onOutput ? <Button kind="secondary" size="small" onClick={() => onOutput(audit)}>Full output</Button> : null}
    </Flex>
  );
}

const SHOW_UNPLACED_KEY = "nvis.postrun.showUnplaced";
/** Tiles drawn per step in the unplaced pool; a few thousand buttons at once make the page crawl. */
const UNPLACED_PAGE = 120;

function loadShowUnplaced(): boolean {
  try {
    return localStorage.getItem(SHOW_UNPLACED_KEY) === "1";
  } catch {
    return false;
  }
}

export function PostRunNodeReportView() {
  const { confirm, confirmDialog } = useConfirm();
  const [data, setData] = useState<PostRunNodeReportResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [section, setSection] = useState<Section>("history");
  const [rack, setRack] = useState("all");
  // Unplaced nodes (in the scheduler, but with no rack in the cluster manager) can be thousands on a
  // fleet that is still being racked, and they drown the real racks. Hidden unless asked for, and
  // remembered like the other view choices.
  const [showUnplaced, setShowUnplaced] = useState<boolean>(loadShowUnplaced);
  const [unplacedLimit, setUnplacedLimit] = useState(UNPLACED_PAGE);
  const [search, setSearch] = useState("");
  const [selectedSnapshot, setSelected] = useState<PostRunNode | null>(null);
  const [category, setCategory] = useState("validation");
  const [reason, setReason] = useState("");
  const [drain, setDrain] = useState(false);
  const [saving, setSaving] = useState(false);
  const [activeAudit, setActiveAudit] = useState<ActiveAudit | null>(null);
  const [outputTitle, setOutputTitle] = useState("");
  const [outputText, setOutputText] = useState("");
  const [outputOpen, setOutputOpen] = useState(false);
  const [outputLoading, setOutputLoading] = useState(false);

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    setError("");
    try {
      const next = await fetchPostRunNodeReport({ refresh });
      setData(next);
      return next;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return null;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(false);
  }, [load]);

  useEffect(() => {
    if (!activeAudit) return;
    let cancelled = false;
    let timer = 0;
    const poll = async () => {
      try {
        const run = await fetchDeepAudit(activeAudit.runId);
        if (cancelled) return;
        if (TERMINAL_AUDIT_STATES.has(run.state)) {
          const report = await load(true);
          if (cancelled) return;
          const exactRunVisible = report?.nodes.some((row) =>
            row.node === activeAudit.node && (
              row.hardware_status?.run_id === activeAudit.runId
              || row.network_status?.run_id === activeAudit.runId
              || row.runs.some((item) => item.activity_id === activeAudit.runId)
            ),
          );
          if (exactRunVisible) {
            setActiveAudit(null);
            return;
          }
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
      if (!cancelled) timer = window.setTimeout(() => void poll(), 1000);
    };
    void poll();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [activeAudit, load]);

  const unplacedCount = useMemo(
    () => (data?.racks || []).filter((g) => g.kind === "unplaced").reduce((n, g) => n + g.nodes.length, 0),
    [data],
  );
  const groups = useMemo(
    () => (data?.racks || []).filter((g) => showUnplaced || g.kind !== "unplaced"),
    [data, showUnplaced],
  );
  const changeShowUnplaced = (on: boolean) => {
    setShowUnplaced(on);
    if (!on && rack === "unplaced") setRack("all");
    try {
      localStorage.setItem(SHOW_UNPLACED_KEY, on ? "1" : "0");
    } catch {
      /* ignore persistence errors */
    }
  };
  // A pool has one card and no racks to choose between, so the picker would offer nothing.
  const showRackFilter = groups.length > 1 || groups.some((g) => g.kind === "rack");
  const selected = useMemo(() => {
    if (!selectedSnapshot) return null;
    return data?.nodes.find((row) => row.node === selectedSnapshot.node) || selectedSnapshot;
  }, [data, selectedSnapshot]);
  const filteredRacks = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return groups
      .filter((row) => rack === "all" || row.rack === rack)
      .map((row) => ({
        ...row,
        nodes: row.nodes.filter((node) => !needle || node.node.toLowerCase().includes(needle)),
      }))
      .filter((row) => row.nodes.length > 0);
  }, [groups, rack, search]);
  // The floor can be thousands of racks; page the groups so the history view is one screen,
  // not an endless scroll. 10 per page by default (the pager still offers larger sizes).
  const pagedRacks = usePagedRows(filteredRacks, 10);

  const openNode = (node: PostRunNode) => {
    setSelected(node);
    setCategory("validation");
    setReason("");
    setDrain(false);
  };

  const launchAudit = async (kind: "hardware" | "network") => {
    if (!selected) return;
    const label = kind === "hardware" ? "hardware" : "NVLink/network";
    if (!(await confirm({
      title: "Run deep audit",
      message: `Run the ${label} deep audit on ${selected.node}? Confirmed failures are automatically drained with the exact finding.`,
      confirmLabel: "Run audit",
    }))) return;
    setSaving(true);
    try {
      const runId = await startDeepAudit({
        workflowId: kind === "hardware" ? "hardware-deep-audit" : "nvlink-network-deep-audit",
        rack: selected.rack,
        target: selected.node,
      });
      setActiveAudit({ runId, kind, node: selected.node });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const openWorkflowOutput = async (runId: string, title: string, stepId = "audit") => {
    setOutputTitle(title);
    setOutputText("");
    setOutputOpen(true);
    setOutputLoading(true);
    try {
      setOutputText(await fetchDeepAuditOutput(runId, stepId));
    } catch (e) {
      setOutputText(e instanceof Error ? e.message : String(e));
    } finally {
      setOutputLoading(false);
    }
  };

  const openAuditOutput = (audit: PostRunAuditStatus) => openWorkflowOutput(
    audit.run_id,
    `${audit.kind} audit ${audit.run_id.slice(0, 8)} · Job ${audit.scheduler_ref || "—"}`,
  );

  const saveMark = async () => {
    if (!selected || !reason.trim()) return;
    if (drain && !(await confirm({
      title: "Drain node",
      message: `Drain ${selected.node} in Slurm and attach this exact reason?`,
      confirmLabel: "Drain",
      danger: true,
    }))) return;
    setSaving(true);
    try {
      await markPostRunNode({ node: selected.node, category, reason: reason.trim(), drain });
      await load(true);
      setSelected(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const sectionNodes = useMemo(
    () => filteredRacks.flatMap((row) => row.nodes),
    [filteredRacks],
  );

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}>
      <PageHeader
        contentId="post-run-node-report"
        title="Post Run Node Report"
        description="Validation participation, evidence-attributed failure trends, repeat flags, deep hardware/network reports, and current Slurm state in one floor-style view."
      />

      {error ? <Banner status="error" kind="inline" style={{ marginBottom: 16 }}>{error}</Banner> : null}
      {data?.refresh.accounting_error ? (
        <Banner status="warning" kind="inline" style={{ marginBottom: 16 }}>
          Live Slurm accounting refresh failed; the durable participation snapshot is still shown: {data.refresh.accounting_error}
        </Banner>
      ) : null}

      {data?.placement_error ? (
        <Banner status="warning" kind="inline" style={{ marginBottom: 16 }}>
          Rack placement is not available yet, so nodes may appear unplaced: {data.placement_error}
        </Banner>
      ) : null}

      <Box sx={{ display: "grid", gridTemplateColumns: "repeat(5, minmax(150px, 1fr))", gap: 1.5, mb: 2 }}>
        {[
          ["Nodes", data?.summary.nodes ?? 0],
          ["Run participations", data?.summary.participations ?? 0],
          ["Attributed failures", data?.summary.attributed_failures ?? 0],
          ["Repeat flags", data?.summary.flagged_nodes ?? 0],
          ["Currently drained", data?.summary.drained_nodes ?? 0],
        ].map(([label, value]) => (
          <Card key={String(label)} style={{ padding: 14 }}>
            <Text kind="body/regular/sm" style={{ display: "block", color: "var(--text-color-secondary)" }}>{label}</Text>
            <Text kind="title/xs">{value}</Text>
          </Card>
        ))}
      </Box>

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Flex gap="2" wrap="wrap" align="center">
          <Button kind={section === "history" ? "primary" : "secondary"} size="small" onClick={() => setSection("history")}>
            <Activity size={15} /> Post-run history
          </Button>
          <Button kind={section === "hardware" ? "primary" : "secondary"} size="small" onClick={() => setSection("hardware")}>
            <ShieldCheck size={15} /> Node hardware status report
          </Button>
          <Button kind={section === "network" ? "primary" : "secondary"} size="small" onClick={() => setSection("network")}>
            <Network size={15} /> Node network status report
          </Button>
          <Box sx={{ flex: 1 }} />
          {showRackFilter && <TextField select size="small" label="Rack" value={rack} onChange={(event) => setRack(event.target.value)} sx={{ minWidth: 190 }}>
            <MenuItem value="all">All racks</MenuItem>
            {groups.map((g) => <MenuItem key={g.rack} value={g.rack}>{g.label || g.rack}</MenuItem>)}
          </TextField>}
          {unplacedCount > 0 && (
            <KuiCheckbox
              checked={showUnplaced}
              onCheckedChange={(v) => changeShowUnplaced(v === true)}
              slotLabel={<Text kind="body/regular/sm">Show unplaced nodes ({unplacedCount.toLocaleString()})</Text>}
            />
          )}
          <TextField
            size="small"
            label="Find node"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <Button kind="secondary" size="small" onClick={() => void load(true)} disabled={loading}>
            <RefreshCw size={15} /> {loading ? "Refreshing…" : "Refresh"}
          </Button>
        </Flex>
      </Paper>

      {section === "history" ? (
        <Flex direction="column" gap="4">
          {pagedRacks.visible.map((rackRow) => (
            <Paper key={rackRow.rack} variant="outlined" sx={{ p: 2 }}>
              <Flex align="center" gap="2" style={{ marginBottom: 12 }}>
                <Text kind="body/bold/md">{rackRow.label || rackRow.rack}</Text>
                {rackRow.scalable_unit ? (
                  <Tag color="blue" kind="outline" readOnly density="compact">{rackRow.scalable_unit.label}</Tag>
                ) : null}
                <Tag color="gray" kind="outline" readOnly density="compact">{rackRow.nodes.length} nodes</Tag>
                {rackRow.kind === "pool" ? (
                  <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                    This cluster has no rack placement, so every node is in one pool.
                  </Text>
                ) : null}
                {rackRow.kind === "unplaced" ? (
                  <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                    These nodes are in the scheduler but the cluster's rack mapping did not place them, so they
                    are listed as a pool.
                  </Text>
                ) : null}
              </Flex>
              <Box sx={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(145px, 1fr))", gap: 1 }}>
                {(rackRow.kind === "unplaced" ? rackRow.nodes.slice(0, unplacedLimit) : rackRow.nodes).map((node) => (
                  <Box
                    key={node.node}
                    component="button"
                    type="button"
                    onClick={() => openNode(node)}
                    title={`${node.node}: ${node.participation_count} runs, ${node.failure_count} attributed findings; Slurm ${node.slurm_state}${node.slurm_reason ? ` — ${node.slurm_reason}` : ""}`}
                    sx={{
                      textAlign: "left",
                      border: node.flagged ? "2px solid #d32f2f" : "1px solid var(--border-color-base)",
                      borderRadius: 1,
                      bgcolor: punchColor(node),
                      color: "inherit",
                      p: 1.25,
                      cursor: "pointer",
                      minHeight: 78,
                      "&:hover": { filter: "brightness(1.08)" },
                    }}
                  >
                    <Flex align="center" gap="1">
                      <Text kind="mono/sm" style={{ flex: 1 }}>{node.node.split("-").slice(-2).join("-")}</Text>
                      <TrendIcon trend={node.trend} />
                      {node.flagged ? <Flag size={14} color="#d32f2f" fill="#d32f2f" /> : null}
                    </Flex>
                    <Text kind="body/regular/sm" style={{ display: "block", marginTop: 8 }}>
                      {node.participation_count} runs · {node.failure_count} failures
                    </Text>
                    <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                      {node.slurm_state}{node.slurm_reason ? " · comment" : ""}
                    </Text>
                  </Box>
                ))}
              </Box>
              {rackRow.kind === "unplaced" && rackRow.nodes.length > unplacedLimit ? (
                <Flex align="center" gap="2" style={{ marginTop: 12 }}>
                  <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                    Showing {unplacedLimit.toLocaleString()} of {rackRow.nodes.length.toLocaleString()}.
                  </Text>
                  <Button kind="secondary" size="small" onClick={() => setUnplacedLimit((n) => n + UNPLACED_PAGE)}>
                    Show {Math.min(UNPLACED_PAGE, rackRow.nodes.length - unplacedLimit).toLocaleString()} more
                  </Button>
                  <Button kind="tertiary" size="small" onClick={() => setUnplacedLimit(rackRow.nodes.length)}>Show all</Button>
                </Flex>
              ) : null}
            </Paper>
          ))}
          <TablePager {...pagedRacks} noun="racks" />
        </Flex>
      ) : (
        <TableContainer component={Paper} variant="outlined">
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                <TableCell>Node</TableCell>
                <TableCell>Rack</TableCell>
                <TableCell>Slurm</TableCell>
                <TableCell>{section === "hardware" ? "Latest hardware audit" : "Latest network / NVLink audit"}</TableCell>
                <TableCell>Action</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {sectionNodes.map((node) => (
                <TableRow key={node.node} hover>
                  <TableCell sx={{ fontFamily: "monospace" }}>{node.node}</TableCell>
                  <TableCell>{node.rack || "—"}</TableCell>
                  <TableCell>
                    <Tag color={stateColor(node.slurm_state)} kind="outline" readOnly density="compact">{node.slurm_state}</Tag>
                    {node.slurm_reason ? <Text kind="body/regular/sm" style={{ display: "block", marginTop: 4 }}>{node.slurm_reason}</Text> : null}
                  </TableCell>
                  <TableCell><AuditCell audit={section === "hardware" ? node.hardware_status : node.network_status} /></TableCell>
                  <TableCell><Button kind="secondary" size="small" onClick={() => openNode(node)}>Results / investigate</Button></TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}

      <Dialog open={Boolean(selected) && !outputOpen} onClose={() => !saving && setSelected(null)} maxWidth="xl" fullWidth>
        {selected ? (
          <>
            <DialogTitle>
              <Flex align="center" gap="2">
                <span style={{ fontFamily: "monospace", flex: 1 }}>{selected.node}</span>
                {selected.flagged ? <Tag color="red" kind="solid" readOnly><Flag size={13} /> repeat offender</Tag> : null}
                <Tag color={stateColor(selected.slurm_state)} kind="outline" readOnly>{selected.slurm_state}</Tag>
              </Flex>
            </DialogTitle>
            <DialogContent dividers>
              <Flex direction="column" gap="4">
                <Paper variant="outlined" sx={{ p: 2 }}>
                  <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 6 }}>Current Slurm comment / reason</Text>
                  <Text kind="mono/sm" style={{ display: "block", whiteSpace: "pre-wrap" }}>
                    {selected.slurm_reason || "No Slurm reason is currently set."}
                  </Text>
                </Paper>

                <Flex gap="2" wrap="wrap">
                  <Button kind="secondary" size="small" onClick={() => void launchAudit("hardware")} disabled={saving}>
                    <Play size={14} /> Run hardware audit
                  </Button>
                  <Button kind="secondary" size="small" onClick={() => void launchAudit("network")} disabled={saving}>
                    <Play size={14} /> Run NVLink / network audit
                  </Button>
                </Flex>

                {activeAudit?.node === selected.node ? (
                  <Banner status="info" kind="inline">
                    Running {activeAudit.kind} audit {activeAudit.runId.slice(0, 8)}. This view will refresh automatically when the Slurm job finishes.
                  </Banner>
                ) : null}

                <Box sx={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 2 }}>
                  <Paper variant="outlined" sx={{ p: 2 }}>
                    <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 8 }}>Latest hardware report</Text>
                    <AuditCell audit={selected.hardware_status} onOutput={(audit) => void openAuditOutput(audit)} />
                  </Paper>
                  <Paper variant="outlined" sx={{ p: 2 }}>
                    <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 8 }}>Latest network report</Text>
                    <AuditCell audit={selected.network_status} onOutput={(audit) => void openAuditOutput(audit)} />
                  </Paper>
                </Box>

                <Box>
                  <Text kind="body/bold/md" style={{ display: "block", marginBottom: 8 }}>
                    Validation participation ({selected.runs.length})
                  </Text>
                  <TableContainer component={Paper} variant="outlined" sx={{ maxHeight: 340 }}>
                    <Table size="small" stickyHeader>
                      <TableHead><TableRow><TableCell>Job ID</TableCell><TableCell>Validation</TableCell><TableCell>State</TableCell><TableCell>Attribution</TableCell><TableCell>When</TableCell><TableCell>Output</TableCell></TableRow></TableHead>
                      <TableBody>
                        {selected.runs.map((run) => (
                          <TableRow key={`${run.job_id}-${run.activity_id}`} hover>
                            <TableCell sx={{ fontFamily: "monospace" }}>{run.job_id || "—"}</TableCell>
                            <TableCell>{run.tool}</TableCell>
                            <TableCell><Tag color={stateColor(run.state)} kind="outline" readOnly density="compact">{run.state}</Tag></TableCell>
                            <TableCell>
                              {run.attributed_failure ? (
                                <><Tag color="red" kind="solid" readOnly density="compact">{run.category}</Tag><Text kind="body/regular/sm" style={{ display: "block", marginTop: 3 }}>{run.reason}</Text></>
                              ) : run.failed_job ? "Job failed; node not implicated" : "No node finding"}
                            </TableCell>
                            <TableCell>{formatWhen(run.started_at)}</TableCell>
                            <TableCell>
                              {run.output_run_id ? (
                                <Button
                                  kind="secondary"
                                  size="small"
                                  onClick={() => void openWorkflowOutput(
                                    run.output_run_id || "",
                                    `${run.tool} ${run.activity_id.slice(0, 8)} · Job ${run.job_id || "—"}`,
                                    run.output_step_id || "audit",
                                  )}
                                >
                                  Full output
                                </Button>
                              ) : "—"}
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </TableContainer>
                </Box>

                {selected.findings.length ? (
                  <Box>
                    <Text kind="body/bold/md" style={{ display: "block", marginBottom: 8 }}>Structured findings ({selected.findings.length})</Text>
                    {selected.findings.map((finding) => (
                      <Paper key={finding.issue_id} variant="outlined" sx={{ p: 1.5, mb: 1 }}>
                        <Flex gap="2" align="center" wrap="wrap">
                          <Tag color="red" kind="outline" readOnly density="compact">{finding.category}</Tag>
                          <Text kind="body/regular/sm">{finding.reason}</Text>
                          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                            Job {finding.scheduler_ref || "—"} · {formatWhen(finding.created_at)} · {finding.operator}
                          </Text>
                        </Flex>
                      </Paper>
                    ))}
                  </Box>
                ) : null}

                <Paper variant="outlined" sx={{ p: 2 }}>
                  <Flex gap="2" align="center" style={{ marginBottom: 12 }}><Wrench size={16} /><Text kind="body/bold/md">Operator mark</Text></Flex>
                  <Box sx={{ display: "grid", gridTemplateColumns: "200px 1fr", gap: 2 }}>
                    <TextField select size="small" label="Category" value={category} onChange={(event) => setCategory(event.target.value)}>
                      {CATEGORY_OPTIONS.map((value) => <MenuItem key={value} value={value}>{value.replace("_", " ")}</MenuItem>)}
                    </TextField>
                    <TextField size="small" multiline minRows={2} label="Evidence-based reason" value={reason} onChange={(event) => setReason(event.target.value)} />
                  </Box>
                  <FormControlLabel
                    control={<Checkbox checked={drain} onChange={(event) => setDrain(event.target.checked)} />}
                    label="Also drain this exact node in Slurm and attach the generated reason"
                  />
                  <Box><Button kind={drain ? "primary" : "secondary"} size="small" onClick={() => void saveMark()} disabled={saving || !reason.trim()}>{saving ? "Saving…" : drain ? "Mark and drain" : "Save mark"}</Button></Box>
                </Paper>
              </Flex>
            </DialogContent>
            <DialogActions><Button kind="secondary" onClick={() => setSelected(null)} disabled={saving}>Close</Button></DialogActions>
          </>
        ) : null}
      </Dialog>
      <Dialog open={outputOpen} onClose={() => setOutputOpen(false)} maxWidth="xl" fullWidth>
        <DialogTitle>{outputTitle || "Deep audit output"}</DialogTitle>
        <DialogContent dividers>
          <Box
            component="pre"
            sx={{ m: 0, p: 2, bgcolor: "#101010", color: "#f2f2f2", overflow: "auto", maxHeight: "70vh", whiteSpace: "pre-wrap" }}
          >
            {outputLoading ? "Loading full output…" : outputText || "No output was recorded."}
          </Box>
        </DialogContent>
        <DialogActions><Button kind="secondary" onClick={() => setOutputOpen(false)}>Close</Button></DialogActions>
      </Dialog>
      {confirmDialog}
    </Box>
  );
}
