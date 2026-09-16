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
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import { Banner, Button, Flex, SegmentedControl, Tag, Text } from "@nvidia/foundations-react-core";
import {
  Download,
  Flag,
  FileText,
  Image,
  Play,
  RefreshCw,
  Wrench,
} from "lucide-react";
import { PageHeader } from "../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import {
  fetchDeepAudit,
  fetchDeepAuditOutput,
  fetchPostRunNodeReport,
  markPostRunNode,
  startDeepAudit,
} from "../services/nodeIssuesService";
import type { PostRunAuditStatus, PostRunNode, PostRunNodeReportResponse } from "../types/api";
import type { BringupFleet } from "../types/bringup";
import { getBringupFleet } from "../services/bringupService";
import { ValidationFloorMap, type FloorNode } from "../components/analytics/ValidationFloorMap";
import {
  availableValidationDomains,
  completionPrediction,
  groupRacksIntoSus,
  latestPassTimestamp,
  nodeValidationState,
  schedulerStatusColor,
  validationTagTone,
  type EtaPrediction,
} from "../lib/validationStatus";
import {
  createValidationCanvas,
  downloadBlob,
  fileSlug,
  jpegDataUrlBytes,
  setJpegDpi,
  validationCsv,
  validationReportPdf,
} from "../lib/validationExport";

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

const SECTION_OPTIONS = [
  { value: "history", children: "Validation map" },
  { value: "hardware", children: "Hardware audits" },
  { value: "network", children: "Network audits" },
];

const STATUS_SWATCH = {
  pass: "var(--color-green-500, #2e7d32)",
  attention: "var(--color-red-500, #c62828)",
  warning: "var(--color-yellow-500, #f9a825)",
  running: "var(--color-blue-500, #1565c0)",
  notRun: "var(--color-yellow-500, #f9a825)",
  unknown: "var(--color-gray-400, #757575)",
};

function LegendSwatch({ color, label }: { color: string; label: string }) {
  return (
    <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
      <Box sx={{ width: 9, height: 9, borderRadius: "2px", backgroundColor: color }} />
      <Text kind="body/regular/sm" style={{ fontSize: 12, color: "var(--text-secondary)" }}>{label}</Text>
    </Box>
  );
}

function formatWhen(value: string): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function stateColor(state: string): "green" | "red" | "yellow" | "blue" | "gray" {
  return schedulerStatusColor(state);
}

function validationTagColor(state: string): "green" | "red" | "yellow" | "gray" {
  if (state === "pass" || state === "attention" || state === "warning" || state === "not_run" || state === "running" || state === "unknown") {
    return validationTagTone(state);
  }
  return schedulerStatusColor(state);
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

export function PostRunNodeReportView() {
  const [data, setData] = useState<PostRunNodeReportResponse | null>(null);
  const [fleet, setFleet] = useState<BringupFleet | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [fleetError, setFleetError] = useState("");
  const [section, setSection] = useState<Section>("history");
  const [mode, setMode] = useState("all");
  const [su, setSu] = useState("all");
  const [lastRefresh, setLastRefresh] = useState("");
  const [rack, setRack] = useState("all");
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
  const loadingRef = useRef(false);

  const load = useCallback(async (refresh = false) => {
    if (loadingRef.current) return null;
    loadingRef.current = true;
    setLoading(true);
    setError("");
    try {
      const next = await fetchPostRunNodeReport({ refresh, compact: true });
      setData(next);
      try {
        const liveFleet = await getBringupFleet(next.nodes.map((row) => row.node));
        setFleet(liveFleet);
        setFleetError("");
      } catch (fleetException) {
        setFleetError(fleetException instanceof Error ? fleetException.message : String(fleetException));
      }
      setLastRefresh(new Date().toISOString());
      return next;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return null;
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(false);
  }, [load]);

  useEffect(() => {
    const timer = window.setInterval(() => void load(true), 30_000);
    return () => window.clearInterval(timer);
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

  const domains = useMemo(() => availableValidationDomains(fleet, data?.nodes || []), [data, fleet]);
  const activeDomain = domains.find((domain) => domain.id === mode) || domains[0];
  const bringupByNode = useMemo(
    () => new Map((fleet?.nodes || []).map((row) => [row.node.toLowerCase(), row])),
    [fleet],
  );
  const selected = selectedSnapshot;
  const reportRacks = useMemo(() => {
    if (data?.racks?.length) return data.racks;
    const grouped = new Map<string, PostRunNode[]>();
    for (const item of data?.nodes || []) grouped.set(item.rack, [...(grouped.get(item.rack) || []), item]);
    return [...grouped.entries()].map(([rackName, nodes]) => ({ rack: rackName, nodes }));
  }, [data]);
  const allSuGroups = useMemo(() => groupRacksIntoSus(reportRacks), [reportRacks]);
  const racks = useMemo(() => allSuGroups.flatMap((group) => group.racks.map((row) => row.rack)), [allSuGroups]);
  const topologyNodeCount = useMemo(
    () => allSuGroups.reduce((total, group) => total + group.racks.reduce((rackTotal, row) => rackTotal + row.nodes.length, 0), 0),
    [allSuGroups],
  );
  const suOptions = useMemo(() => allSuGroups.map((group) => ({ id: group.id, rowKey: group.rowKey })), [allSuGroups]);
  const filteredSuGroups = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return allSuGroups
      .filter((group) => su === "all" || group.id === su)
      .map((group) => ({
        ...group,
        racks: group.racks
          .filter((row) => rack === "all" || row.rack === rack)
          .map((row) => ({
            ...row,
            nodes: row.nodes.filter((node) => !needle || node.node.toLowerCase().includes(needle)),
          }))
          .filter((row) => row.nodes.length > 0),
      }))
      .filter((group) => group.racks.length > 0);
  }, [allSuGroups, rack, search, su]);
  const filteredRacks = useMemo(() => filteredSuGroups.flatMap((group) => group.racks), [filteredSuGroups]);
  const floorGroups = useMemo(() => filteredSuGroups.map((group) => ({
    ...group,
    racks: group.racks.map((rackRow) => ({
      ...rackRow,
      nodes: rackRow.nodes.map((node) => ({
        node,
        state: nodeValidationState(node, bringupByNode.get(node.node.toLowerCase()), mode),
      })),
    })),
  })), [bringupByNode, filteredSuGroups, mode]);
  const predictionBySu = useMemo(() => {
    const predictions = new Map<string, EtaPrediction>();
    const now = lastRefresh ? new Date(lastRefresh) : new Date();
    for (const group of floorGroups) {
      const states = group.racks.flatMap((rackRow) => rackRow.nodes).map((entry) => ({
        state: entry.state,
        timestamp: latestPassTimestamp(entry.state),
      }));
      predictions.set(group.id, completionPrediction(states, now));
    }
    return predictions;
  }, [floorGroups, lastRefresh]);
  const clusterPrediction = useMemo(() => completionPrediction(
    floorGroups.flatMap((group) => group.racks.flatMap((rackRow) => rackRow.nodes)).map((entry) => ({
      state: entry.state,
      timestamp: latestPassTimestamp(entry.state),
    })),
    lastRefresh ? new Date(lastRefresh) : new Date(),
  ), [floorGroups, lastRefresh]);
  const sourceFreshness = `post-run ${formatWhen(data?.generated_at || "")} · bring-up ${formatWhen(lastRefresh)} · DCGM index ${formatWhen(data?.refresh.dcgm_observed_at || "")} (${data?.refresh.dcgm_events || 0} attempts) · health-check index ${formatWhen(data?.refresh.healthcheck_observed_at || "")} (${data?.refresh.healthcheck_events || 0} nodes)`;
  const selectedState = selected
    ? nodeValidationState(selected, bringupByNode.get(selected.node.toLowerCase()), mode)
    : null;

  const openNode = (node: PostRunNode) => {
    setSelected(node);
    setCategory("validation");
    setReason("");
    setDrain(false);
    void fetchPostRunNodeReport({ node: node.node }).then((full) => {
      const exact = full.nodes.find((item) => item.node === node.node);
      if (exact) setSelected(exact);
    }).catch((exception) => setError(exception instanceof Error ? exception.message : String(exception)));
  };

  const launchAudit = async (kind: "hardware" | "network") => {
    if (!selected) return;
    const label = kind === "hardware" ? "hardware" : "NVLink/network";
    if (!window.confirm(`Run the ${label} deep audit on ${selected.node}? Confirmed failures are automatically drained with the exact finding.`)) return;
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
    if (drain && !window.confirm(`Drain ${selected.node} through the active scheduler and attach this exact reason?`)) return;
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

  const exportItems = useMemo(() => floorGroups.flatMap((group) =>
    group.racks.flatMap((rackRow) => rackRow.nodes.map((entry) => ({
      node: entry.node,
      su: group.id,
      state: entry.state,
      prediction: predictionBySu.get(group.id) || clusterPrediction,
    }))),
  ), [clusterPrediction, floorGroups, predictionBySu]);

  const exportStamp = () => new Date().toISOString().replace(/[:.]/g, "-");
  const exportBase = () => `validation-status-${fileSlug(activeDomain.label)}-${exportStamp()}`;
  const mapOptions = () => ({
    groups: floorGroups as Array<{
      id: string;
      rowKey: string;
      racks: Array<{ rack: string; nodes: FloorNode[] }>;
    }>,
    modeLabel: activeDomain.label,
    generatedAt: lastRefresh || new Date().toISOString(),
    freshness: sourceFreshness,
    clusterPrediction,
  });
  const downloadCsv = () => {
    const csv = validationCsv(exportItems, activeDomain.label, lastRefresh || new Date().toISOString(), sourceFreshness);
    downloadBlob(new Blob([csv], { type: "text/csv;charset=utf-8" }), `${exportBase()}.csv`);
  };
  const downloadJpeg = () => {
    const dataUrl = createValidationCanvas(mapOptions()).toDataURL("image/jpeg", 0.94);
    const jpeg = setJpegDpi(jpegDataUrlBytes(dataUrl));
    downloadBlob(new Blob([jpeg.buffer as ArrayBuffer], { type: "image/jpeg" }), `${exportBase()}.jpg`);
  };
  const downloadPdf = () => {
    const dataUrl = createValidationCanvas(mapOptions()).toDataURL("image/jpeg", 0.94);
    const pdf = validationReportPdf(
      setJpegDpi(jpegDataUrlBytes(dataUrl)),
      exportItems,
      activeDomain.label,
      lastRefresh || new Date().toISOString(),
      sourceFreshness,
      clusterPrediction,
    );
    downloadBlob(new Blob([pdf.buffer as ArrayBuffer], { type: "application/pdf" }), `${exportBase()}.pdf`);
  };

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}>
      <PageHeader
        contentId="post-run-node-report"
        title="Validation Status"
        description="Live node health and validation progress across every SU and rack."
      />

      {error ? <Banner status="error" kind="inline" style={{ marginBottom: 16 }}>{error}</Banner> : null}
      {data?.refresh.accounting_error ? (
        <Banner status="warning" kind="inline" style={{ marginBottom: 16 }}>
          Live scheduler history refresh failed; the durable participation snapshot is still shown: {data.refresh.accounting_error}
        </Banner>
      ) : null}
      {fleetError ? (
        <Banner status="warning" kind="inline" style={{ marginBottom: 16 }}>
          Node-health evidence is unavailable; missing checks remain unknown, never healthy: {fleetError}
        </Banner>
      ) : null}
      {data?.refresh.healthcheck_error ? (
        <Banner status="warning" kind="inline" style={{ marginBottom: 16 }}>
          Single-node health-check evidence is unavailable; affected nodes remain unknown: {data.refresh.healthcheck_error}
        </Banner>
      ) : null}

      <Paper variant="outlined" sx={{ px: 1.5, py: 1.25, mb: 2 }}>
        <Flex gap="0" wrap="wrap" align="center">
        {[
          ["Nodes", topologyNodeCount],
          ["Run participations", data?.summary.participations ?? 0],
          ["Attributed failures", data?.summary.attributed_failures ?? 0],
          ["Repeat flags", data?.summary.flagged_nodes ?? 0],
          ["Currently drained", data?.summary.drained_nodes ?? 0],
        ].map(([label, value], index) => (
          <Box
            key={String(label)}
            sx={{
              minWidth: 145,
              px: 1.5,
              py: 0.25,
              borderLeft: index ? "1px solid var(--border-subtle)" : "none",
            }}
          >
            <Text kind="body/regular/sm" style={{ color: "var(--text-secondary)" }}>{label}</Text>{" "}
            <Text kind="body/bold/sm">{value}</Text>
          </Box>
        ))}
        </Flex>
      </Paper>

      <Paper variant="outlined" sx={{ p: 1.5, mb: 2 }}>
        <Flex gap="2" wrap="wrap" align="center" style={{ marginBottom: 10 }}>
          <SegmentedControl
            size="small"
            items={SECTION_OPTIONS}
            value={section}
            onValueChange={(value) => setSection(value as Section)}
            aria-label="Validation report section"
          />
          <Box sx={{ flex: 1 }} />
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            Auto-refresh 30s · last {formatWhen(lastRefresh)}
          </Text>
          <Button kind="secondary" size="small" onClick={() => void load(true)} disabled={loading}>
            <RefreshCw size={15} /> {loading ? "Refreshing…" : "Refresh"}
          </Button>
        </Flex>
        <Flex gap="2" wrap="wrap" align="center">
          <TextField select size="small" label="Validation view" value={mode} onChange={(event) => setMode(event.target.value)} sx={{ minWidth: 210 }}>
            {domains.map((domain) => <MenuItem key={domain.id} value={domain.id}>{domain.label}</MenuItem>)}
          </TextField>
          <TextField select size="small" label="SU" value={su} onChange={(event) => setSu(event.target.value)} sx={{ minWidth: 130 }}>
            <MenuItem value="all">All 14 SUs</MenuItem>
            {suOptions.map((option) => <MenuItem key={option.id} value={option.id}>{option.id} · {option.rowKey}</MenuItem>)}
          </TextField>
          <TextField select size="small" label="Rack" value={rack} onChange={(event) => setRack(event.target.value)} sx={{ minWidth: 190 }}>
            <MenuItem value="all">All racks</MenuItem>
            {racks.map((name) => <MenuItem key={name} value={name}>{name}</MenuItem>)}
          </TextField>
          <TextField
            size="small"
            label="Find node"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <Button kind="secondary" size="small" onClick={downloadCsv} disabled={!exportItems.length}>
            <Download size={14} /> CSV
          </Button>
          <Button kind="secondary" size="small" onClick={downloadPdf} disabled={!exportItems.length}>
            <FileText size={14} /> PDF
          </Button>
          <Button kind="secondary" size="small" onClick={downloadJpeg} disabled={!exportItems.length}>
            <Image size={14} /> JPEG 11×8.5
          </Button>
        </Flex>
      </Paper>

      {section === "history" ? (
        <Flex direction="column" gap="2">
          <Box sx={{ display: "flex", flexWrap: "wrap", gap: 2, alignItems: "center" }}>
              <Text kind="body/bold/sm">{activeDomain.label}</Text>
              <LegendSwatch color={STATUS_SWATCH.pass} label="Passed" />
              <LegendSwatch color={STATUS_SWATCH.attention} label="Failed / blocked" />
              <LegendSwatch color={STATUS_SWATCH.warning} label="Incomplete" />
              <LegendSwatch color={STATUS_SWATCH.running} label="Running" />
              <LegendSwatch color={STATUS_SWATCH.notRun} label="Not run" />
              <LegendSwatch color={STATUS_SWATCH.unknown} label="No evidence" />
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                Segments are individual nodes. Hover for evidence; select a rack to open its highest-priority node.
              </Text>
          </Box>
          <Paper
            variant="outlined"
            sx={{
              px: 1.5,
              py: 1,
              borderLeft: `4px solid ${clusterPrediction.state === "complete" ? STATUS_SWATCH.pass : clusterPrediction.state === "blocked" ? STATUS_SWATCH.warning : "var(--color-blue-500, #1565c0)"}`,
            }}
          >
            <Flex gap="2" align="center" wrap="wrap">
              <Text kind="body/bold/sm">Cluster prediction</Text>
              <Tag color={clusterPrediction.state === "complete" ? "green" : clusterPrediction.state === "blocked" ? "yellow" : "blue"} kind="outline" readOnly density="compact">
                {clusterPrediction.label}
              </Tag>
              <Text kind="body/regular/sm">
                {clusterPrediction.completed}/{clusterPrediction.total} pass · {clusterPrediction.running} running · {clusterPrediction.confidence} confidence
              </Text>
              <Text kind="body/regular/sm" style={{ color: "var(--text-secondary)" }}>{clusterPrediction.methodology}</Text>
            </Flex>
          </Paper>
          <ValidationFloorMap
            groups={floorGroups}
            onNode={openNode}
          />
        </Flex>
      ) : (
        <TableContainer component={Paper} variant="outlined">
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                <TableCell>Node</TableCell>
                <TableCell>Rack</TableCell>
                <TableCell>Scheduler</TableCell>
                <TableCell>{section === "hardware" ? "Latest hardware audit" : "Latest network / NVLink audit"}</TableCell>
                <TableCell>Action</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {sectionNodes.map((node) => (
                <TableRow key={node.node} hover>
                  <TableCell sx={{ fontFamily: "monospace" }}>{node.node}</TableCell>
                  <TableCell>{node.rack}</TableCell>
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
                  <Flex gap="2" align="center" wrap="wrap" style={{ marginBottom: 10 }}>
                    <Text kind="body/bold/md" style={{ flex: 1 }}>{activeDomain.label} evidence</Text>
                    <Tag color={validationTagColor(selectedState?.status || "unknown")} kind="solid" readOnly>
                      {(selectedState?.status || "unknown").replace("_", " ")}
                    </Tag>
                    {selectedState?.running ? <Tag color="yellow" kind="outline" readOnly>live job running</Tag> : null}
                  </Flex>
                  {selectedState?.points.length ? (
                    <TableContainer sx={{ maxHeight: 300 }}>
                      <Table size="small" stickyHeader>
                        <TableHead><TableRow><TableCell>Test / check</TableCell><TableCell>Status</TableCell><TableCell>Metric</TableCell><TableCell>Job ID</TableCell><TableCell>Evidence / failure reason</TableCell><TableCell>Updated</TableCell></TableRow></TableHead>
                        <TableBody>
                          {selectedState.points.map((point) => (
                            <TableRow key={point.id} hover>
                              <TableCell>{point.name}<Text kind="body/regular/sm" style={{ display: "block", color: "var(--text-color-secondary)" }}>{point.source}</Text></TableCell>
                              <TableCell><Tag color={validationTagColor(point.status)} kind="outline" readOnly density="compact">{point.rawStatus || point.status}</Tag></TableCell>
                              <TableCell>{point.metric ? `${point.metric}${point.metricUnit ? ` ${point.metricUnit}` : ""}` : "—"}</TableCell>
                              <TableCell sx={{ fontFamily: "monospace" }}>{point.jobId || "—"}</TableCell>
                              <TableCell>
                                {point.reason || (point.status === "pass" ? "Passed" : "No reason was reported.")}
                                {point.provenance ? <Text kind="mono/sm" style={{ display: "block", color: "var(--text-color-secondary)", overflowWrap: "anywhere" }}>{point.provenance}</Text> : null}
                              </TableCell>
                              <TableCell>{formatWhen(point.timestamp)}</TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </TableContainer>
                  ) : (
                    <Banner status="warning" kind="inline">No evidence was reported for this node in the selected view. It is unknown, not healthy.</Banner>
                  )}
                </Paper>
                <Paper variant="outlined" sx={{ p: 2 }}>
                  <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 6 }}>Current scheduler comment / reason</Text>
                  <Text kind="mono/sm" style={{ display: "block", whiteSpace: "pre-wrap" }}>
                    {selected.slurm_reason || "No scheduler reason is currently set."}
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
                    Running {activeAudit.kind} audit {activeAudit.runId.slice(0, 8)}. This view will refresh automatically when the scheduler task finishes.
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
                    label="Also drain this exact node through the active scheduler and attach the generated reason"
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
    </Box>
  );
}
