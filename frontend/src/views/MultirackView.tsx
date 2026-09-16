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
import { useState, useMemo, useEffect, useCallback, useRef } from "react";
import Box from "@mui/material/Box";
import MuiButton from "@mui/material/Button";
import Checkbox from "@mui/material/Checkbox";
import FormControlLabel from "@mui/material/FormControlLabel";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Paper from "@mui/material/Paper";
import Snackbar from "@mui/material/Snackbar";
import Alert from "@mui/material/Alert";
import CircularProgress from "@mui/material/CircularProgress";
import TextField from "@mui/material/TextField";
import InputAdornment from "@mui/material/InputAdornment";
import { Card, Text, Tag, Button, Banner, Flex } from "@nvidia/foundations-react-core";
import { attachLaunchOptions, type LaunchOptions } from "../lib/rackCommandBodies";
import { LaunchOptionsDialog } from "../components/rack/LaunchOptionsDialog";
import { MultiSelectCombobox } from "../components/MultiSelectCombobox";
import { Layers, LineChart, Gauge, Search } from "lucide-react";
import Tooltip from "@mui/material/Tooltip";
import { PageHeader } from "../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { ParserResultsDialog } from "../components/ParserResultsDialog";
import { ValidationActivitiesTable } from "../components/rack/ValidationActivitiesTable";
import { ValidationPresetDialog } from "../components/rack/ValidationPresetDialog";
import { TailLogDialog } from "../components/TailLogDialog";
import {
  buildMultirackTailBody,
  openRackLogInNewTab,
  validationOngoing,
  type TailLogStreamBody,
} from "../lib/rackLog";
import { validationStatusTooltip } from "../lib/validationStatusTooltip";
import { apiFetch } from "../lib/apiFetch";
import { VALIDATION_CATEGORIES, nemoCategory } from "../lib/validationStages";
import { buildRackRowMap } from "../lib/rackTopology";
import type { MultirackJob, RackRow, RackStatusResponse } from "../types/api";
import { usePagedRows } from "../hooks/usePagedRows";
import { TablePager } from "../components/TablePager";
import {
  VALIDATION_PRESET_LABELS,
  type ValidationPresetId,
  type ValidationPresetRequest,
} from "../lib/validationPresets";

interface MultirackViewProps {
  data: RackStatusResponse | null;
  logDir: string;
}

function isRackEligible(row: RackRow): { eligible: boolean; reason: string } {
  if (row.nemo !== "done") return { eligible: false, reason: "need validation done" };
  const counts = new Map<string, number>();
  for (const part of row.sinfo.split(",")) {
    const match = /^\s*([a-z_]+)\s*=\s*(\d+)\s*$/i.exec(part);
    if (match) counts.set(match[1].toLowerCase(), Number(match[2]));
  }
  const idle = counts.get("idle") ?? 0;
  if (idle < 16) return { eligible: false, reason: `requires 16 idle nodes; found ${idle}` };
  return { eligible: true, reason: "" };
}

type TagColor = "green" | "red" | "yellow" | "blue" | "gray";

function nemoColor(nemo: string): TagColor {
  if (nemo === "done") return "green";
  if (nemo === "fail") return "red";
  if (nemo.startsWith("ongoing")) return "yellow";
  if (nemo === "pending") return "blue";
  return "gray";
}

function MultirackValidationTag({ row }: { row: MultirackStatusRow }) {
  const tip = validationStatusTooltip({
    nemo: row.nemo,
    last_job: row.last_job,
    nemo_hint: row.nemo_hint,
  });
  const tag = (
    <Tag color={nemoColor(row.nemo)} kind="solid" readOnly density="compact">
      {row.nemo}
    </Tag>
  );
  if (!tip) return tag;
  return (
    <Tooltip title={<span style={{ display: "block", maxWidth: 420, whiteSpace: "pre-wrap" }}>{tip}</span>} enterDelay={350} arrow>
      <span style={{ display: "inline-flex", cursor: "default" }}>{tag}</span>
    </Tooltip>
  );
}

interface MultirackStatusRow {
  log_file: string;
  display_rack: string;
  nemo: string;
  last_job: string;
  sinfo: string;
  nemo_hint?: string;
}

interface MultirackCampaignRack {
  rack: string;
  eligible: boolean;
  reason: string;
  nodes?: string[];
  node_count?: number;
}

interface MultirackCampaignResponse {
  ok: boolean;
  active: boolean;
  id?: string;
  error?: string;
  racks?: MultirackCampaignRack[];
  eligible_count?: number;
  rack_count?: number;
  nodes_per_rack?: number;
  source?: "manifest" | "live-scheduler";
}

/** log_file from API is usually a basename; strip any path for parser ?log_file= */
function logBasenameForParser(logFile: string): string {
  const s = logFile.replace(/\\/g, "/").trim();
  if (!s) return "";
  const i = s.lastIndexOf("/");
  return i >= 0 ? s.slice(i + 1) : s;
}

type MultirackLaunchKind = "nemo" | "llmb";

const LAUNCH_META: Record<MultirackLaunchKind, { label: string; asyncUrl: string }> = {
  nemo: { label: "Multirack NEMO validation", asyncUrl: "/api/run-nemo-xrack-async" },
  llmb: { label: "NCCL Microbenchmark (direct)", asyncUrl: "/api/run-llmb-nccl-async" },
};

const TERMINAL_PHASES = new Set(["done", "error", "timeout"]);

const LAUNCH_TITLE: Record<MultirackLaunchKind, string> = {
  nemo: "Launch Multirack NEMO Validation",
  llmb: "Launch NCCL Microbenchmark",
};

const EMPTY_RACK_ROWS: RackRow[] = [];

export function MultirackView({ data, logDir }: MultirackViewProps) {
  const [dryRun, setDryRun] = useState(false);
  const [pendingKind, setPendingKind] = useState<MultirackLaunchKind | null>(null);
  const [pendingPreset, setPendingPreset] = useState<ValidationPresetId | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [campaign, setCampaign] = useState<MultirackCampaignResponse | null>(null);
  const [campaignError, setCampaignError] = useState("");

  // Rack picker filters. 112 racks over 5 pages is unusable for "launch on row H" without them.
  const [query, setQuery] = useState("");
  const [rowFilters, setRowFilters] = useState<string[]>([]);
  const [validationFilters, setValidationFilters] = useState<string[]>([]);
  const [eligibleOnly, setEligibleOnly] = useState(false);
  const [launchingKind, setLaunchingKind] = useState<MultirackLaunchKind | null>(null);
  const [launchingPreset, setLaunchingPreset] = useState<ValidationPresetId | null>(null);
  const [snackbar, setSnackbar] = useState<{ open: boolean; message: string; severity: "success" | "error" | "info" }>({
    open: false, message: "", severity: "info",
  });

  // Launched jobs — used to refresh on-disk results when a run finishes.
  const [jobs, setJobs] = useState<MultirackJob[]>([]);

  // Multirack status (on-disk logs / parser results)
  const [mrRows, setMrRows] = useState<MultirackStatusRow[]>([]);
  // Grows with every multirack run that leaves a log behind.
  const mrPage = usePagedRows(mrRows);
  const [mrLoading, setMrLoading] = useState(false);
  const [mrError, setMrError] = useState("");

  const [parserOpen, setParserOpen] = useState(false);
  const [parserRack, setParserRack] = useState("");
  const [parserLogBasename, setParserLogBasename] = useState<string | undefined>();
  const [parserJobId, setParserJobId] = useState<string | undefined>();
  const [parserInitialTab, setParserInitialTab] = useState(1);

  const [tailStream, setTailStream] = useState<{ title: string; body: TailLogStreamBody } | null>(null);

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const prevTerminalRef = useRef(0);

  const openMultirackParser = useCallback((row: MultirackStatusRow, initialTab: number) => {
    const bn = logBasenameForParser(row.log_file);
    if (!bn) return;
    setParserRack(row.display_rack || "");
    setParserLogBasename(bn);
    setParserJobId(row.last_job && row.last_job !== "-" ? row.last_job : undefined);
    setParserInitialTab(initialTab);
    setParserOpen(true);
  }, []);

  const openMultirackLog = useCallback(
    async (row: MultirackStatusRow) => {
      const bn = logBasenameForParser(row.log_file);
      if (!bn) {
        setSnackbar({ open: true, message: "No log file for this row.", severity: "error" });
        return;
      }
      const r = await openRackLogInNewTab(logDir, { log_file: bn });
      if (!r.ok) {
        setSnackbar({ open: true, message: r.error, severity: "error" });
      }
    },
    [logDir]
  );

  const startMultirackTail = useCallback(
    (row: MultirackStatusRow) => {
      const bn = logBasenameForParser(row.log_file);
      if (!bn) {
        setSnackbar({ open: true, message: "No log file for this row.", severity: "error" });
        return;
      }
      setTailStream({
        title: `Tail log — ${row.display_rack || bn}`,
        body: buildMultirackTailBody(logDir, bn),
      });
    },
    [logDir]
  );

  // Fetch multirack status (defined before the poll effect so the poller can refresh results).
  const fetchMultirackStatus = useCallback(async () => {
    setMrLoading(true);
    setMrError("");
    try {
      const res = await apiFetch(`/api/multirack-status?log_dir=${encodeURIComponent(logDir)}`);
      const body = await res.text();
      let json: { ok?: boolean; rows?: Record<string, string>[]; error?: string };
      try {
        json = JSON.parse(body) as { ok?: boolean; rows?: Record<string, string>[]; error?: string };
      } catch {
        // HTML 500 / SPA index from a crashed or missing route — don't surface raw parse noise.
        const looksHtml = /^\s*<(?:!doctype|html)/i.test(body);
        setMrError(
          looksHtml
            ? `/api/multirack-status returned HTML instead of JSON (HTTP ${res.status}). ` +
              "The backend likely crashed building the on-disk results table — check container logs."
            : `Non-JSON response from /api/multirack-status (HTTP ${res.status}): ${body.slice(0, 120)}`
        );
        setMrRows([]);
        return;
      }
      if (json.ok && Array.isArray(json.rows)) {
        setMrRows(json.rows.map((r: Record<string, string>) => ({
          log_file: r.log_file || r.basename || "",
          display_rack: r.rack || r.display_rack || r.rack_display || "",
          nemo: r.nemo || "-",
          last_job: r.last_job || "-",
          sinfo: r.sinfo || "-",
          nemo_hint: r.nemo_hint || "",
        })));
      } else {
        setMrRows([]);
        if (json.error) setMrError(json.error);
      }
    } catch (err) {
      setMrError(err instanceof Error ? err.message : "Failed to fetch");
      setMrRows([]);
    } finally {
      setMrLoading(false);
    }
  }, [logDir]);

  // Stable empty fallback: `|| []` builds a new array every render, which would re-slice the
  // pager's memo each time and defeats the point of memoising it.
  const rows = data?.rows || EMPTY_RACK_ROWS;

  const eligibilityMap = useMemo(() => {
    const map = new Map<string, { eligible: boolean; reason: string }>();
    if (campaign?.active && (!campaign.ok || !Array.isArray(campaign.racks))) {
      for (const row of rows) map.set(row.rack, { eligible: false, reason: "exact-node campaign unavailable" });
      return map;
    }
    if (campaign?.active && Array.isArray(campaign.racks)) {
      const campaignRows = new Map(campaign.racks.map((row) => [row.rack, row]));
      for (const row of rows) {
        const found = campaignRows.get(row.rack);
        map.set(
          row.rack,
          found
            ? { eligible: found.eligible, reason: found.reason }
            : { eligible: false, reason: "not in active exact-node campaign" },
        );
      }
      return map;
    }
    for (const row of rows) {
      map.set(row.rack, isRackEligible(row));
    }
    return map;
  }, [rows, campaign]);

  const eligibleCount = useMemo(() => {
    let count = 0;
    for (const v of eligibilityMap.values()) {
      if (v.eligible) count++;
    }
    return count;
  }, [eligibilityMap]);

  // Row label comes from the backend hierarchy, so "row" means whatever the site's naming
  // template calls that slot (d for ink50-dh01-d16, a for bos01-a09) with no id parsing here.
  const rackRowMap = useMemo(() => buildRackRowMap(data), [data]);

  const rowOptions = useMemo(() => {
    const set = new Set<string>();
    for (const r of rows) {
      const label = rackRowMap.get(r.rack);
      if (label) set.add(label);
    }
    return [...set]
      .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
      .map((v) => ({ value: v, children: v }));
  }, [rows, rackRowMap]);

  const filteredRows = useMemo(() => {
    let result = rows;
    if (query.trim()) {
      const q = query.trim().toLowerCase();
      result = result.filter((r) => r.rack.toLowerCase().includes(q));
    }
    if (rowFilters.length > 0) {
      const set = new Set(rowFilters);
      result = result.filter((r) => set.has(rackRowMap.get(r.rack) ?? ""));
    }
    if (validationFilters.length > 0) {
      const set = new Set(validationFilters);
      result = result.filter((r) => set.has(nemoCategory(r.nemo)));
    }
    if (eligibleOnly) {
      result = result.filter((r) => eligibilityMap.get(r.rack)?.eligible);
    }
    return result;
  }, [rows, query, rowFilters, validationFilters, eligibleOnly, rackRowMap, eligibilityMap]);

  // One row per rack in the fleet, and this is the picker you scroll to choose from. Paging the
  // FILTERED list keeps the pager's range text honest about what is on screen.
  const rackPage = usePagedRows(filteredRows);

  const filtersActive =
    query.trim() !== "" || rowFilters.length > 0 || validationFilters.length > 0 || eligibleOnly;

  const clearFilters = () => {
    setQuery("");
    setRowFilters([]);
    setValidationFilters([]);
    setEligibleOnly(false);
  };

  // Eligible racks WITHIN the current filter: "select all eligible" that ignored the filter
  // would silently queue racks the operator cannot see.
  const filteredEligible = useMemo(
    () => filteredRows.filter((r) => eligibilityMap.get(r.rack)?.eligible).map((r) => r.rack),
    [filteredRows, eligibilityMap]
  );

  // A selection made before filtering can end up off-screen, and the launch buttons count it.
  const hiddenSelectedCount = useMemo(() => {
    if (selected.size === 0) return 0;
    const visible = new Set(filteredRows.map((r) => r.rack));
    let n = 0;
    for (const rack of selected) if (!visible.has(rack)) n++;
    return n;
  }, [selected, filteredRows]);

  const handleToggle = (rack: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(rack)) next.delete(rack);
      else next.add(rack);
      return next;
    });
  };

  // Scoped to the filtered set: with a row filter applied, "select all eligible" means the racks
  // on screen. Toggles off only those, leaving any selection made under a different filter alone.
  const allFilteredEligibleSelected =
    filteredEligible.length > 0 && filteredEligible.every((rack) => selected.has(rack));

  const handleSelectAllEligible = () => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allFilteredEligibleSelected) {
        for (const rack of filteredEligible) next.delete(rack);
      } else {
        for (const rack of filteredEligible) next.add(rack);
      }
      return next;
    });
  };

  // Fetch the durable launched-jobs table (shared across users/sessions).
  const fetchJobs = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/multirack-jobs?log_dir=${encodeURIComponent(logDir)}`);
      const json = await res.json();
      if (json.ok && Array.isArray(json.jobs)) {
        setJobs(json.jobs as MultirackJob[]);
      }
    } catch {
      // keep last-known jobs on transient errors
    }
  }, [logDir]);

  const fetchCampaign = useCallback(async () => {
    try {
      const response = await apiFetch("/api/multirack-campaign");
      const body = (await response.json()) as MultirackCampaignResponse;
      if (body.ok) {
        setCampaign(body);
        setCampaignError("");
        // A selection can be made against stock eligibility before the live exact-node
        // campaign arrives. Prune it at the snapshot boundary so no effect has to mirror
        // derived state and a stale target cannot reach the launch dialog.
        if (body.active && Array.isArray(body.racks)) {
          const eligible = new Set(body.racks.filter((row) => row.eligible).map((row) => row.rack));
          setSelected((previous) => {
            const next = new Set([...previous].filter((rack) => eligible.has(rack)));
            return next.size === previous.size ? previous : next;
          });
        }
      } else {
        setCampaign(body.active ? body : null);
        setCampaignError(body.error || "Exact-node campaign eligibility is unavailable");
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "Exact-node campaign eligibility is unavailable";
      setCampaign((previous) => previous?.active ? { ...previous, ok: false, error: message } : null);
      setCampaignError(message);
    }
  }, []);

  const handleLaunch = async (kind: MultirackLaunchKind, opts: LaunchOptions) => {
    if (selected.size === 0 || launchingKind) return;
    if (campaign?.active && !campaign.ok) {
      setSnackbar({ open: true, message: "Exact-node campaign eligibility is unavailable; launch blocked.", severity: "error" });
      return;
    }
    const meta = LAUNCH_META[kind];
    const racks = [...selected].sort();
    setLaunchingKind(kind);
    try {
      const body: Record<string, unknown> = { racks, log_dir: logDir };
      if (kind === "llmb") body.dry_run = dryRun;
      if (campaign?.active && campaign.id) body.campaign = campaign.id;
      attachLaunchOptions(body, opts);
      const res = await apiFetch(meta.asyncUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const json = await res.json();
      if (json.ok) {
        setSnackbar({ open: true, message: `${meta.label} launched (${racks.length} racks)`, severity: "success" });
        setSelected(new Set());
        fetchJobs();
      } else {
        setSnackbar({ open: true, message: json.error || "Launch failed", severity: "error" });
      }
    } catch (err) {
      setSnackbar({ open: true, message: `Network error: ${err instanceof Error ? err.message : "unknown"}`, severity: "error" });
    } finally {
      setLaunchingKind(null);
    }
  };

  const confirmLaunch = (opts: LaunchOptions) => {
    const kind = pendingKind;
    setPendingKind(null);
    if (kind) void handleLaunch(kind, opts);
  };

  const launchPreset = async (request: ValidationPresetRequest) => {
    const presetId = request.preset_id;
    setLaunchingPreset(presetId);
    try {
      const response = await apiFetch("/api/run-validation-preset-async", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
      });
      const body = (await response.json()) as { ok?: boolean; error?: string };
      if (!response.ok || !body.ok) {
        throw new Error(body.error || `Launch failed (HTTP ${response.status})`);
      }
      setSnackbar({
        open: true,
        message: `${VALIDATION_PRESET_LABELS[presetId]} launched on ${request.racks?.length ?? 0} racks`,
        severity: "success",
      });
      setSelected(new Set());
      void fetchJobs();
    } finally {
      setLaunchingPreset(null);
    }
  };

  // Poll launched jobs; refresh on-disk results whenever a job newly reaches a terminal phase.
  useEffect(() => {
    fetchJobs();
    fetchMultirackStatus();
    fetchCampaign();
    pollRef.current = setInterval(() => {
      void fetchJobs();
      void fetchCampaign();
    }, 6000);
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [fetchJobs, fetchMultirackStatus, fetchCampaign]);

  useEffect(() => {
    const terminal = jobs.filter((j) => TERMINAL_PHASES.has(j.phase)).length;
    if (terminal > prevTerminalRef.current) {
      fetchMultirackStatus();
    }
    prevTerminalRef.current = terminal;
  }, [jobs, fetchMultirackStatus]);

  const noRacks = !data || rows.length === 0;
  const selectedCampaignRows = useMemo(() => {
    if (!campaign?.active || !campaign.ok || !Array.isArray(campaign.racks)) return [];
    const byRack = new Map(campaign.racks.map((row) => [row.rack, row]));
    return [...selected].map((rack) => byRack.get(rack)).filter((row): row is MultirackCampaignRack => Boolean(row));
  }, [campaign, selected]);
  const selectedCampaignIsExact =
    selected.size > 0 &&
    selectedCampaignRows.length === selected.size &&
    selectedCampaignRows.every((row) =>
      row.eligible &&
      ((typeof row.node_count === "number" && row.node_count > 0) || (Array.isArray(row.nodes) && row.nodes.length > 0)),
    );
  const selectedCampaignNodeCount = selectedCampaignIsExact
    ? selectedCampaignRows.reduce(
        (total, row) => total + (typeof row.node_count === "number" ? row.node_count : row.nodes?.length ?? 0),
        0,
      )
    : 0;
  const llama1024Ready = selectedCampaignIsExact && selectedCampaignNodeCount === 256;

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}>
      <PageHeader
        contentId="multirack"
        title="Multirack Validation"
        description="Cross-rack NEMO, NCCL, DGXC system_info, GPT-OSS per-node fan-out, and fixed 256-node / 1024-GPU, 50-step Llama 3.1 405B FP8 validation."
      />

      {/* Inline rack selection + launch */}
      <Card style={{ marginBottom: 24, padding: 20 }}>
        <Flex gap="2" align="center" wrap="wrap" style={{ marginBottom: 12 }}>
          <Text kind="title/sm" style={{ marginRight: "auto" }}>
            Available racks
          </Text>
          <Tag color={eligibleCount > 0 ? "green" : "gray"} kind="outline" readOnly density="compact">
            {eligibleCount} eligible
          </Tag>
          {selected.size > 0 && (
            <Tag color="blue" kind="solid" readOnly density="compact">
              {selected.size} selected
            </Tag>
          )}
        </Flex>

        {campaign?.active && campaign.ok ? (
          <div style={{ marginBottom: 12 }}>
            <Banner status="info">
              Exact-node multirack selection active: {campaign.eligible_count ?? 0} of {campaign.rack_count ?? 0} racks eligible, {campaign.nodes_per_rack ?? 16} selected nodes per rack. All multirack validations use only these {campaign.source === "manifest" ? "campaign manifest" : "live scheduler"} nodes.
            </Banner>
          </div>
        ) : null}
        {campaignError ? (
          <div style={{ marginBottom: 12 }}>
            <Banner status="warning">
              Exact-node campaign unavailable: {campaignError}. {campaign?.active ? "Campaign launch is blocked." : "Stock eligibility is shown."}
            </Banner>
          </div>
        ) : null}

        <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
          <Flex gap="4" wrap="wrap" align="end">
            <div style={{ minWidth: 200, flex: "1 1 220px" }}>
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 4, display: "block" }}>
                Rack
              </Text>
              <TextField
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Filter by rack id…"
                size="small"
                slotProps={{
                  input: {
                    startAdornment: (
                      <InputAdornment position="start">
                        <Search size={16} />
                      </InputAdornment>
                    ),
                  },
                }}
                fullWidth
              />
            </div>

            {rowOptions.length > 0 && (
              <div style={{ minWidth: 160 }}>
                <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 4, display: "block" }}>
                  Row
                </Text>
                <MultiSelectCombobox
                  items={rowOptions}
                  value={rowFilters}
                  onValueChange={setRowFilters}
                  size="small"
                  aria-label="Filter by datacenter row"
                />
              </div>
            )}

            <div style={{ minWidth: 180 }}>
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 4, display: "block" }}>
                Validation
              </Text>
              <MultiSelectCombobox
                items={VALIDATION_CATEGORIES}
                value={validationFilters}
                onValueChange={setValidationFilters}
                size="small"
                aria-label="Filter by validation status"
              />
            </div>

            <FormControlLabel
              control={
                <Checkbox checked={eligibleOnly} onChange={(e) => setEligibleOnly(e.target.checked)} size="small" />
              }
              label="Eligible only"
            />

            {filtersActive && (
              <Button kind="secondary" size="small" onClick={clearFilters}>
                Clear filters
              </Button>
            )}
          </Flex>

          {filtersActive && (
            <Text
              kind="body/regular/sm"
              style={{ color: "var(--text-color-secondary)", marginTop: 8, display: "block" }}
            >
              {filteredRows.length} of {rows.length} racks shown
              {filteredEligible.length > 0 ? ` — ${filteredEligible.length} eligible here` : " — none eligible here"}
            </Text>
          )}
        </Paper>

        {hiddenSelectedCount > 0 && (
          <div style={{ marginBottom: 12 }}>
            <Banner status="warning">
              {hiddenSelectedCount} selected rack{hiddenSelectedCount === 1 ? " is" : "s are"} hidden by the current
              filter and would still be included in a launch.
            </Banner>
          </div>
        )}

        <Flex gap="2" align="center" wrap="wrap" style={{ marginBottom: 12 }}>
          <MuiButton
            size="small"
            variant="outlined"
            onClick={handleSelectAllEligible}
            disabled={filteredEligible.length === 0}
          >
            {allFilteredEligibleSelected
              ? `Clear selection (${filteredEligible.length})`
              : `Select all eligible (${filteredEligible.length})`}
          </MuiButton>
          <FormControlLabel
            control={<Checkbox checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} size="small" />}
            label="Dry run (NCCL/Llama — print plan, don't submit)"
          />
          <Flex gap="2" align="center" wrap="wrap" style={{ marginLeft: "auto" }}>
            <Button
              kind="primary"
              size="small"
              onClick={() => setPendingKind("nemo")}
              disabled={noRacks || selected.size === 0 || launchingKind !== null}
            >
              <Layers size={16} style={{ marginRight: 6 }} />
              {launchingKind === "nemo" ? "Launching…" : `Launch Multirack Validation (${selected.size})`}
            </Button>
            <Button
              kind="secondary"
              size="small"
              onClick={() => setPendingKind("llmb")}
              disabled={noRacks || selected.size === 0 || launchingKind !== null}
            >
              <Gauge size={16} style={{ marginRight: 6 }} />
              {launchingKind === "llmb" ? "Launching…" : `Launch NCCL Microbenchmark (${selected.size})`}
            </Button>
          </Flex>
        </Flex>

        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 12, display: "block" }}>
          {campaign?.active && campaign.ok
            ? `Eligibility is computed from exactly ${campaign.nodes_per_rack ?? 16} idle nodes per rack; extra or unavailable trays do not disqualify the rack. Every multirack launcher receives that exact nodelist.`
            : "Eligible racks require at least 16 idle nodes. Single-rack validation continues to use the full 18-node rack."}
        </Text>

        <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
          <Flex gap="2" align="center" wrap="wrap" style={{ marginBottom: 8 }}>
            <Text kind="body/bold/md" style={{ marginRight: "auto" }}>Pinned validation recipes</Text>
            <Tag color="green" kind="outline" readOnly density="compact">prepare → review → launch</Tag>
          </Flex>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block", marginBottom: 12 }}>
            Server-validated versions and image inventory. PythonCM is used where the recipe has an equivalent; recipes requiring allocation, MPI, Pyxis, or LLMB semantics use the documented Slurm fallback.
          </Text>
          <Flex gap="2" wrap="wrap">
            <Button kind="secondary" size="small" disabled={selected.size === 0 || launchingPreset !== null} onClick={() => setPendingPreset("nccl-2.29.3")}>NCCL 2.29.3 · 8K–32G · 500 iterations</Button>
            <Button kind="secondary" size="small" disabled={!llama1024Ready || launchingPreset !== null} onClick={() => setPendingPreset("llama31-405b-fp8")}>Llama 405B FP8 — 1024 GPUs / 50 steps</Button>
            <Button kind="secondary" size="small" disabled={selected.size === 0 || launchingPreset !== null} onClick={() => setPendingPreset("gpt-oss-cpu")}>GPT-OSS CPU — per-node fan-out</Button>
            <Button kind="secondary" size="small" disabled={selected.size === 0 || launchingPreset !== null} onClick={() => setPendingPreset("exemplar-system-info")}>DGXC system_info</Button>
            <Button kind="secondary" size="small" disabled={selected.size === 0 || launchingPreset !== null} onClick={() => setPendingPreset("nemo-26.04.01")}>NeMo 26.04.01</Button>
            <Button kind="secondary" size="small" disabled={selected.size === 0 || launchingPreset !== null} onClick={() => setPendingPreset("mnubergemm-26.03-v1.17")}>MNUberGEMM 26.03-v1.17</Button>
          </Flex>
          {selected.size > 0 && !llama1024Ready ? (
            <div style={{ marginTop: 12 }}>
              <Banner status="info">
                The fixed Llama preset requires exactly 256 manifest nodes / 1024 GB300 GPUs. {campaign?.active && campaign.ok
                  ? `${selectedCampaignNodeCount} manifest nodes are selected; adjust the rack selection before preparing.`
                  : "An active exact-node campaign is required to prove the selected node count."}
              </Banner>
            </div>
          ) : null}
        </Paper>

        {noRacks ? (
          <Card style={{ padding: 32, textAlign: "center" }}>
            <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
              No rack status available yet. Rack data loads from the dashboard poll.
            </Text>
          </Card>
        ) : filteredRows.length === 0 ? (
          <Card style={{ padding: 32, textAlign: "center" }}>
            <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)", display: "block" }}>
              No racks match the current filter.
            </Text>
            <div style={{ marginTop: 12 }}>
              <Button kind="secondary" size="small" onClick={clearFilters}>
                Clear filters
              </Button>
            </div>
          </Card>
        ) : (
          <TableContainer component={Paper} variant="outlined" sx={{ maxHeight: "50vh" }}>
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  <TableCell padding="checkbox" />
                  <TableCell>Rack</TableCell>
                  <TableCell>Validation</TableCell>
                  <TableCell>Node availability</TableCell>
                  <TableCell>Status</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {rackPage.visible.map((row) => {
                  const { eligible, reason } = eligibilityMap.get(row.rack) || { eligible: false, reason: "unknown" };
                  return (
                    <TableRow
                      key={row.rack}
                      hover={eligible}
                      onClick={() => eligible && handleToggle(row.rack)}
                      sx={{
                        cursor: eligible ? "pointer" : "default",
                        opacity: eligible ? 1 : 0.5,
                      }}
                    >
                      <TableCell padding="checkbox">
                        <Checkbox
                          checked={selected.has(row.rack)}
                          disabled={!eligible}
                          slotProps={{ input: { "aria-label": `Select rack ${row.rack}` } }}
                          onClick={(e) => e.stopPropagation()}
                          onChange={() => handleToggle(row.rack)}
                        />
                      </TableCell>
                      <TableCell sx={{ fontWeight: 600 }}>{row.rack}</TableCell>
                      <TableCell>
                        <MultirackValidationTag
                          row={{
                            log_file: "",
                            display_rack: row.rack,
                            nemo: row.nemo,
                            last_job: row.last_job,
                            sinfo: row.sinfo,
                            nemo_hint: row.nemo_hint,
                          }}
                        />
                      </TableCell>
                      <TableCell>
                        <Text kind="body/regular/sm">{row.sinfo}</Text>
                      </TableCell>
                      <TableCell>
                        {eligible ? (
                          <Tag color="green" kind="outline" readOnly density="compact">eligible</Tag>
                        ) : (
                          <Text kind="body/regular/sm" style={{ color: "var(--text-color-subtle)" }}>
                            {reason}
                          </Text>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </TableContainer>
        )}
        <TablePager {...rackPage} noun="racks" />
      </Card>

      <ValidationActivitiesTable
        logDir={logDir}
        scope="multirack"
        title="Validation activities"
        logRows={mrRows}
        onOpenParser={(row, initialTab) =>
          openMultirackParser(row as MultirackStatusRow, initialTab)
        }
      />

      {/* On-disk results & logs (parsed from the log directory) */}
      <Text kind="title/sm" style={{ display: "block", marginBottom: 12 }}>
        Results &amp; logs on disk
      </Text>

      {mrLoading && (
        <Box sx={{ display: "flex", justifyContent: "center", py: 6 }}>
          <CircularProgress />
        </Box>
      )}

      {mrError && (
        <div style={{ marginBottom: 16 }}>
          <Banner status="warning">
            {mrError}
          </Banner>
        </div>
      )}

      {!mrLoading && mrRows.length > 0 && (
        <TableContainer component={Paper} variant="outlined">
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                <TableCell>Racks</TableCell>
                <TableCell>Validation</TableCell>
                <TableCell>Last Job</TableCell>
                <TableCell>Node availability</TableCell>
                <TableCell>Log File</TableCell>
                <TableCell align="right">Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {mrPage.visible.map((row, i) => {
                const hasLog = Boolean(logBasenameForParser(row.log_file));
                const live = validationOngoing(row.nemo);
                return (
                  <TableRow key={i} hover>
                    <TableCell sx={{ fontWeight: 600 }}>{row.display_rack}</TableCell>
                    <TableCell>
                      <MultirackValidationTag row={row} />
                    </TableCell>
                    <TableCell>
                      <Text kind="body/regular/md" style={{ fontFamily: "monospace" }}>{row.last_job}</Text>
                    </TableCell>
                    <TableCell>
                      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>{row.sinfo}</Text>
                    </TableCell>
                    <TableCell>
                      <Text kind="mono/sm" style={{ color: "var(--text-color-secondary)" }}>
                        {row.log_file.length > 50 ? "..." + row.log_file.slice(-47) : row.log_file}
                      </Text>
                    </TableCell>
                    <TableCell align="right">
                      <Box sx={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 0.5, flexWrap: "wrap" }}>
                        {live ? (
                          <MuiButton
                            size="small"
                            variant="outlined"
                            disabled={!hasLog}
                            onClick={() => startMultirackTail(row)}
                          >
                            Tail Log
                          </MuiButton>
                        ) : (
                          <MuiButton
                            size="small"
                            variant="outlined"
                            disabled={!hasLog}
                            onClick={() => void openMultirackLog(row)}
                          >
                            Open Log
                          </MuiButton>
                        )}
                        <Tooltip title={hasLog ? "NCCL / NEMO charts and Grafana (parser.sh --json)" : "No log file"}>
                          <span>
                            <MuiButton
                              size="small"
                              variant="contained"
                              disabled={!hasLog}
                              startIcon={<LineChart size={16} />}
                              onClick={() => openMultirackParser(row, 1)}
                            >
                              View Results
                            </MuiButton>
                          </span>
                        </Tooltip>
                      </Box>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableContainer>
      )}
      <TablePager {...mrPage} noun="runs" />

      {!mrLoading && mrRows.length === 0 && !mrError && (
        <Card style={{ padding: 48, textAlign: "center" }}>
          <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
            No multi-rack logs found on disk yet. Completed runs land here with links to their charts, Grafana, and raw logs.
          </Text>
        </Card>
      )}

      {/* Snackbar */}
      <Snackbar
        open={snackbar.open}
        autoHideDuration={5000}
        onClose={() => setSnackbar((s) => ({ ...s, open: false }))}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      >
        <Alert
          onClose={() => setSnackbar((s) => ({ ...s, open: false }))}
          severity={snackbar.severity}
          variant="filled"
          sx={{ width: "100%" }}
        >
          {snackbar.message}
        </Alert>
      </Snackbar>

      <TailLogDialog
        open={tailStream !== null}
        onClose={() => setTailStream(null)}
        title={tailStream?.title ?? "Tail log"}
        payload={tailStream?.body ?? null}
      />

      <LaunchOptionsDialog
        open={pendingKind !== null}
        onClose={() => setPendingKind(null)}
        onConfirm={confirmLaunch}
        title={pendingKind ? LAUNCH_TITLE[pendingKind] : "Launch options"}
        subtitle={`${selected.size} rack(s) selected${pendingKind === "llmb" && dryRun ? " — dry run" : ""}`}
        confirmLabel="Launch"
        showNcclVersion={pendingKind === "llmb"}
        showIters={pendingKind === "llmb"}
        showMaxSteps={pendingKind === "nemo"}
      />

      {pendingPreset ? (
        <ValidationPresetDialog
          open
          onClose={() => setPendingPreset(null)}
          presetId={pendingPreset}
          scope="multirack"
          racks={selected}
          logDir={logDir}
          campaignId={campaign?.id}
          onLaunch={launchPreset}
        />
      ) : null}

      <ParserResultsDialog
        open={parserOpen}
        onClose={() => {
          setParserOpen(false);
          setParserLogBasename(undefined);
          setParserJobId(undefined);
          setParserInitialTab(1);
        }}
        rack={parserRack}
        jobId={parserJobId}
        logDir={logDir}
        logFileBasename={parserLogBasename}
        initialTabIndex={parserInitialTab}
      />
    </Box>
  );
}
