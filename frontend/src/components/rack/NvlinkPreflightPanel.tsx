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
// NvlinkPreflightPanel.tsx — NVLink/NVLS readiness gate, run before Stage 1.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Sits at the top of the Validation tab because it is the cheap check that decides whether the
// expensive ones are worth starting: a wedged NVSwitch SDN route makes NCCL/NeMo collectives
// hang, and this catches it in minutes instead of a burnt 36-node run.
//
// The UI deliberately distinguishes "inconclusive" from "ready". The script's functional
// data-path probe is the only check that detects the wedged route, and it self-skips when its
// venv is absent — so a node whose probe was skipped is shown amber, never green.
import { useCallback, useEffect, useRef, useState } from "react";
import Box from "@mui/material/Box";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import MuiButton from "@mui/material/Button";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Paper from "@mui/material/Paper";
import FormControlLabel from "@mui/material/FormControlLabel";
import Checkbox from "@mui/material/Checkbox";
import {
  Accordion,
  AnimatedChevron,
  Banner,
  Button,
  Card,
  Collapsible,
  Flex,
  ProgressBar,
  Spinner,
  Tag,
  Text,
} from "@nvidia/foundations-react-core";
import { ShieldCheck } from "lucide-react";
import { ActionCard } from "./ActionCard";
import { CollapsibleLog } from "../CollapsibleLog";
import { apiFetch } from "../../lib/apiFetch";
import { validationTargetFromNodes } from "../../lib/validationTargetFromNodes";
import type { NvisNamingSchema } from "../../types/naming";

const POLL_MS = 2000;
/** The full gate allows 120s per node plus pdsh fan-out; give it room before giving up. */
const POLL_MAX_MS = 35 * 60 * 1000;

type NodeStatus = "pass" | "fail" | "incomplete" | "no_response" | "error";

interface PreflightCheck {
  name: string;
  state: "pass" | "fail" | "warn" | "skipped";
  detail: string;
}

interface PreflightRow {
  host: string;
  status: NodeStatus;
  result_detail: string;
  started: boolean;
  quick: boolean | null;
  checks: PreflightCheck[];
  passed: number;
  failed: number;
  warned: number;
  skipped: number;
  first_failure: string;
  transport_error: string;
}

interface PreflightSummary {
  total: number;
  passed: number;
  failed: number;
  incomplete: number;
  no_response: number;
  /** Informational: nodes that passed but did not run every check. Does not block ready. */
  nodes_with_skips: number;
  nodes_with_warnings: number;
  ready: boolean;
  worst: string;
}

interface JobStatus {
  ok?: boolean;
  phase?: string;
  error?: string | null;
  rows?: PreflightRow[];
  summary?: PreflightSummary;
  stdout?: string;
  stderr?: string;
  command_display?: string;
  quick?: boolean;
}

type TagColor = "green" | "red" | "yellow" | "blue" | "gray";

// Only a failed check turns a node red. A warning (SHARP unprovisioned -> NCCL falls back to
// NVLS_TREE) and a skipped check are reported in the detail but do not change the verdict.
// Missing data is not a warning, so a node that never answered or never finished is not green.
const STATUS_TAG: Record<NodeStatus, { color: TagColor; label: string }> = {
  pass: { color: "green", label: "ready" },
  fail: { color: "red", label: "not ready" },
  incomplete: { color: "yellow", label: "incomplete" },
  no_response: { color: "gray", label: "no response" },
  error: { color: "red", label: "unreachable" },
};

const CHECK_TAG: Record<PreflightCheck["state"], TagColor> = {
  pass: "green",
  fail: "red",
  warn: "yellow",
  skipped: "gray",
};

interface NvlinkPreflightPanelProps {
  rack: string;
  naming?: NvisNamingSchema;
  selectedNodes: Set<string>;
  disabled: boolean;
}

/** ``1m 12s`` — an elapsed readout so a multi-minute wait is visibly progressing, not stuck. */
function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m > 0 ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

export function NvlinkPreflightPanel({ rack, naming, selectedNodes, disabled }: NvlinkPreflightPanelProps) {
  const [quick, setQuick] = useState(false);
  const [running, setRunning] = useState(false);
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<JobStatus | null>(null);
  const [error, setError] = useState("");
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const cancelled = useRef(false);
  const target = validationTargetFromNodes(rack, selectedNodes, naming) || "";

  const run = useCallback(async () => {
    setRunning(true);
    setError("");
    setStatus(null);
    setStartedAt(Date.now());
    setElapsed(0);
    cancelled.current = false;
    try {
      const body: Record<string, unknown> = { rack, quick };
      if (target) body.target = target;
      const res = await apiFetch("/api/nvlink-preflight-async", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const started = (await res.json()) as { ok?: boolean; job_id?: string; error?: string };
      if (!res.ok || !started.ok || !started.job_id) {
        throw new Error(started.error || "failed to start the preflight");
      }
      setOpen(true);
      const deadline = Date.now() + POLL_MAX_MS;
      while (Date.now() < deadline && !cancelled.current) {
        await new Promise((r) => setTimeout(r, POLL_MS));
        const sres = await apiFetch(
          `/api/reset-job-status?job_id=${encodeURIComponent(started.job_id)}`,
        );
        const data = (await sres.json()) as JobStatus;
        setStatus(data);
        const phase = String(data.phase || "").toLowerCase();
        if (phase === "done" || phase === "error") return;
      }
      if (!cancelled.current) setError("timed out waiting for the preflight to finish");
    } catch (e) {
      setError(e instanceof Error ? e.message : "preflight failed to start");
      setOpen(true);
    } finally {
      setRunning(false);
    }
  }, [rack, quick, target]);

  const rows = status?.rows ?? [];
  const summary = status?.summary;
  const phase = String(status?.phase || "").toLowerCase();
  const inFlight = running && phase !== "done" && phase !== "error";

  // Tick the elapsed readout only while a run is in flight. The initial 0 is set by the click
  // handler, so the effect body sets no state — it only owns the interval.
  useEffect(() => {
    if (!inFlight || startedAt === null) return;
    const t = setInterval(() => setElapsed(Date.now() - startedAt), 1000);
    return () => clearInterval(t);
  }, [inFlight, startedAt]);

  return (
    <>
      <ActionCard
        title="NVLink Preflight — run this first"
        category="validation"
        description={
          `Per-node NVLink / NVLS readiness gate on ${selectedNodes.size} selected node(s) for ${rack || target}. ` +
          "Checks GPU enumeration, 18 NVLinks, the P2P matrix, fabric registration, IMEX domain and dmesg, " +
          "and (full mode) forces an NVLink all_reduce under timeout with a RoCE control leg — the only check " +
          "that detects a wedged NVSwitch route, which otherwise makes NCCL and NeMo hang. Run before Stage 1."
        }
        // Horizontal, and the only card in the app that asks for it by name: this is one
        // prominent full-width card above the board, not a cell in a grid, and its description
        // is a paragraph. KUI documents `horizontal` for exactly that — content-led cards that
        // stand alone — and the wide text column is what keeps the summary line readable.
        orientation="horizontal"
        subtitle={`${selectedNodes.size} node(s) · ${quick ? "quick" : "full"} mode`}
        icon={<ShieldCheck size={20} />}
        buttonLabel={running ? "Checking…" : "Run Preflight"}
        status={running ? "running" : summary ? (summary.ready ? "success" : "error") : "idle"}
        disabled={disabled || running}
        onRun={run}
      />

      <Box sx={{ mt: -1, mb: 1, ml: 1 }}>
        <FormControlLabel
          control={
            <Checkbox
              size="small"
              checked={quick}
              disabled={running}
              onChange={(e) => setQuick(e.target.checked)}
            />
          }
          label={
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              Quick mode — skip the NVLink data-path probe (faster, but will not catch a wedged NVSwitch route)
            </Text>
          }
        />
      </Box>

      <Dialog
        open={open}
        onClose={() => {
          cancelled.current = true;
          setOpen(false);
        }}
        maxWidth="lg"
        fullWidth
      >
        <DialogTitle>NVLink Preflight — {rack || target}</DialogTitle>
        <DialogContent dividers>
          {error && (
            <Box sx={{ mb: 2 }}>
              <Banner status="error">{error}</Banner>
            </Box>
          )}

          {inFlight && (
            <Box sx={{ mb: 2 }}>
              <Banner status="info">
                {quick
                  ? "Running the quick gate on every selected node…"
                  : "Running the full gate. The forced-NVLink probe allows up to 120s per node; nodes are checked in parallel."}
              </Banner>
              {/* Indeterminate rather than a percentage: the nodes are checked in one parallel
                  pdsh pass, so no partial count exists until it returns. KUI reserves Spinner
                  for 1-5s waits and points at a bar for longer ones, and this runs for minutes. */}
              <Box sx={{ mt: 1.5 }}>
                <ProgressBar
                  kind="indeterminate"
                  aria-label={`NVLink preflight running on ${rack || target}`}
                />
              </Box>
              <Flex align="center" gap="2" style={{ marginTop: 8 }}>
                <Spinner size="small" aria-label="Preflight in progress" />
                <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                  {`Running for ${formatElapsed(elapsed)} — ${selectedNodes.size} node(s)`}
                  {quick ? "" : ". Typically 1-3 minutes."}
                </Text>
              </Flex>
              <Text
                kind="body/regular/sm"
                style={{ display: "block", marginTop: 6, color: "var(--text-color-subtle)" }}
              >
                Closing this dialog stops watching; the check keeps running on the cluster.
              </Text>
            </Box>
          )}

          {summary && (
            <Box sx={{ mb: 2 }}>
              <Banner status={summary.ready ? "success" : summary.failed ? "error" : "warning"}>
                {summary.ready
                  ? `All ${summary.total} node(s) NVLS-ready — safe to start Stage 1.`
                  : `${summary.passed}/${summary.total} node(s) ready. ` +
                    [
                      summary.failed ? `${summary.failed} failed` : "",
                      summary.incomplete ? `${summary.incomplete} incomplete` : "",
                      summary.no_response ? `${summary.no_response} did not respond` : "",
                    ]
                      .filter(Boolean)
                      .join(", ")}
              </Banner>
              {/* Coverage note, not a blocker: the verdict above already stands. This says which
                  checks did not run, so a green rack does not imply the data path was proven. */}
              {(summary.nodes_with_skips > 0 || summary.nodes_with_warnings > 0) && (
                <Text
                  kind="body/regular/sm"
                  style={{ display: "block", marginTop: 6, color: "var(--text-color-secondary)" }}
                >
                  {[
                    summary.nodes_with_skips
                      ? `${summary.nodes_with_skips} node(s) had a check skipped` +
                        (quick ? " (quick mode skips the NVLink data-path probe)" : "")
                      : "",
                    summary.nodes_with_warnings
                      ? `${summary.nodes_with_warnings} node(s) raised a warning`
                      : "",
                  ]
                    .filter(Boolean)
                    .join(" · ") + " — see per-node checks below."}
                </Text>
              )}
            </Box>
          )}

          {rows.length > 0 && (
            <TableContainer component={Paper} variant="outlined" sx={{ mb: 2 }}>
              <Table size="small" stickyHeader aria-label={`NVLink preflight results for ${rack}`}>
                <TableHead>
                  <TableRow>
                    <TableCell>Node</TableCell>
                    <TableCell>Result</TableCell>
                    <TableCell>Checks</TableCell>
                    <TableCell>Reason</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {rows.map((r) => {
                    const tag = STATUS_TAG[r.status] ?? STATUS_TAG.no_response;
                    return (
                      <TableRow key={r.host} hover>
                        <TableCell sx={{ fontWeight: 600, fontFamily: "monospace" }}>{r.host}</TableCell>
                        <TableCell>
                          <Tag color={tag.color} kind="solid" readOnly density="compact">
                            {tag.label}
                          </Tag>
                        </TableCell>
                        <TableCell>
                          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                            {`${r.passed} pass`}
                            {r.failed ? `, ${r.failed} fail` : ""}
                            {r.warned ? `, ${r.warned} warn` : ""}
                            {r.skipped ? `, ${r.skipped} skipped` : ""}
                          </Text>
                        </TableCell>
                        <TableCell>
                          <Text kind="body/regular/sm" style={{ color: "var(--text-color-subtle)" }}>
                            {r.first_failure || r.transport_error || r.result_detail || "—"}
                          </Text>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </TableContainer>
          )}

          {rows.length > 0 && (
            <Card style={{ padding: 12, marginBottom: 16 }}>
              {/* One container for the whole per-node block: 18 flat rows otherwise dominate the
                  dialog and push the raw log off-screen. Collapsible rather than an outer
                  Accordion because KUI advises against nesting Accordions. Collapsed by default —
                  the table above already carries each node's verdict and reason. */}
              <Collapsible
                slotTrigger={
                  <Button asChild kind="secondary" color="neutral" size="small">
                    <div style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      {`Per-node checks (${rows.length} node${rows.length === 1 ? "" : "s"})`}
                      <AnimatedChevron />
                    </div>
                  </Button>
                }
              >
                {/* Single-open: an operator inspects one node at a time, per the KUI guidance. */}
                <Accordion
                  items={rows.map((r) => ({
                    value: r.host,
                    slotTrigger: (
                      // Host first in a fixed-width column so the tags form a straight edge.
                      // Tag-first left the hostnames ragged, because "ready", "inconclusive" and
                      // "unreachable" are different widths.
                      <Flex align="center" gap="inherit">
                        <Text kind="mono/sm" style={{ minWidth: 210, textAlign: "left" }}>
                          {r.host}
                        </Text>
                        <Tag
                          color={(STATUS_TAG[r.status] ?? STATUS_TAG.no_response).color}
                          kind="solid"
                          readOnly
                          density="compact"
                        >
                          {(STATUS_TAG[r.status] ?? STATUS_TAG.no_response).label}
                        </Tag>
                        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                          {`${r.passed} pass`}
                          {r.failed ? `, ${r.failed} fail` : ""}
                          {r.warned ? `, ${r.warned} warn` : ""}
                          {r.skipped ? `, ${r.skipped} skipped` : ""}
                        </Text>
                      </Flex>
                    ),
                    slotContent: r.checks.length ? (
                      <Box sx={{ display: "flex", flexDirection: "column", gap: 0.75, py: 1 }}>
                        {r.checks.map((c, i) => (
                          // Same ordering rule as the trigger: fixed-width name column first,
                          // then the state tag, so the tags line up down the list.
                          <Flex key={`${c.name}-${i}`} gap="2" align="start" wrap="nowrap">
                            <Text kind="mono/sm" style={{ minWidth: 190, flexShrink: 0 }}>
                              {c.name}
                            </Text>
                            <Box sx={{ flexShrink: 0, minWidth: 86 }}>
                              <Tag color={CHECK_TAG[c.state]} kind="outline" readOnly density="compact">
                                {c.state}
                              </Tag>
                            </Box>
                            <Text
                              kind="body/regular/sm"
                              style={{ color: "var(--text-color-secondary)", flex: 1, minWidth: 0 }}
                            >
                              {c.detail || "—"}
                            </Text>
                          </Flex>
                        ))}
                      </Box>
                    ) : (
                      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                        {r.transport_error
                          ? `No checks ran — ${r.transport_error}`
                          : "This node reported no checks."}
                      </Text>
                    ),
                  }))}
                />
              </Collapsible>
            </Card>
          )}

          {(status?.command_display || status?.stdout || status?.stderr) && (
            <Card style={{ padding: 12 }}>
              <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 8 }}>
                Raw output
              </Text>
              {status?.command_display && (
                <Text
                  kind="mono/sm"
                  style={{ display: "block", color: "var(--text-color-subtle)", marginBottom: 8 }}
                >
                  {status.command_display}
                </Text>
              )}
              <Box sx={{ display: "flex", flexDirection: "column", gap: 1, alignItems: "flex-start" }}>
                {status?.stdout ? (
                  <CollapsibleLog text={status.stdout} label="preflight output" maxHeight={420} />
                ) : null}
                {status?.stderr ? (
                  <CollapsibleLog text={status.stderr} label="stderr" tone="error" maxHeight={240} />
                ) : null}
              </Box>
            </Card>
          )}
        </DialogContent>
        <DialogActions>
          <MuiButton
            onClick={() => {
              cancelled.current = true;
              setOpen(false);
            }}
          >
            Close
          </MuiButton>
        </DialogActions>
      </Dialog>
    </>
  );
}
