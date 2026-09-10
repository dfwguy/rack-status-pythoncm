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
import { useState, useEffect } from "react";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import Tabs from "@mui/material/Tabs";
import Tab from "@mui/material/Tab";
import Box from "@mui/material/Box";
import CircularProgress from "@mui/material/CircularProgress";
import FormControl from "@mui/material/FormControl";
import InputLabel from "@mui/material/InputLabel";
import Select from "@mui/material/Select";
import MenuItem from "@mui/material/MenuItem";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Paper from "@mui/material/Paper";
import { Button, Text, Banner, Card, Flex, Tag, Checkbox } from "@nvidia/foundations-react-core";
import { RefreshCw } from "lucide-react";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  LogarithmicScale,
  LineElement,
  PointElement,
  Title,
  Tooltip,
  Legend,
  Filler,
} from "chart.js";
import { Line } from "react-chartjs-2";
import { ParserGrafanaEmbeds, type GrafanaEmbedPayload } from "./ParserGrafanaEmbeds";
import { NvloomHeatmapsSection } from "./NvloomHeatmapsSection";
import { ClusterkitResultsSection, type ClusterkitRunRow } from "./ClusterkitResultsSection";
import { usePagedRows } from "../hooks/usePagedRows";
import { TablePager } from "../components/TablePager";
import { apiFetch } from "../lib/apiFetch";

ChartJS.register(
  CategoryScale,
  LinearScale,
  LogarithmicScale,
  LineElement,
  PointElement,
  Title,
  Tooltip,
  Legend,
  Filler,
);

/** localStorage key carried over from the pre-SPA UI — renaming it drops the operator's saved timezone. */
const DISPLAY_TZ_KEY = "rackWebDisplayTimezone";
/** Persist "Full rack validation (18N)" preference across opens. */
const FULL_RACK_KEY = "rackWebParserFullRack";

/** Re-parse cadence while a sweep is still being written. Long enough not to hammer a 10 MB log. */
const PARTIAL_REPARSE_MS = 20000;

function browserIana(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
}

/** IANA string for /api/parser-results?display_timezone= (empty = omit). */
function chartsTzForApi(stored: string): string {
  const u = (stored || "").trim();
  if (!u) return "";
  if (u === "browser") return browserIana() || "";
  return u;
}

function initFullRackFromStorage(): boolean {
  try {
    const raw = localStorage.getItem(FULL_RACK_KEY);
    if (raw === null) return true; // default ON — avoid partial 1N sweeps looking like rack results
    return raw === "1" || raw.toLowerCase() === "true";
  } catch {
    return true;
  }
}

export interface ParserResultsDialogProps {
  open: boolean;
  onClose: () => void;
  rack: string;
  jobId?: string;
  logDir?: string;
  /**
   * Multirack (or explicit) log basename under log_dir — same as legacy openParserResults(..., logFile).
   * When set, request uses log_file=… and optional rack= for Grafana context (job_id omitted unless also provided).
   */
  logFileBasename?: string;
  /**
   * Explicit allowlisted log path (e.g. a Travis bring-up node log
   * ``results/<node>/allreduce.txt``). Highest precedence: parses that exact file
   * via ``/api/parser-results?path=…``, bypassing the rack/basename resolution.
   */
  logPath?: string;
  /**
   * 0 = Summary, 1 = NCCL, 2 = NEMO. The rest SHIFT with what the server returned: LLAMA takes 3
   * only when it has rows, so NVLOOM is 4 with a LLAMA tab and 3 without (``nvloomTabIndex``), and
   * ClusterKit follows it when runs exist. Do not hardcode 4/5 — compute from the same flags.
   */
  initialTabIndex?: number;
}

interface NcclPoint {
  size_bytes: number;
  bus_bw_gbps: number;
}

interface NcclRow {
  job_id: string;
  rack: string;
  peak_busbw_gbps: number;
  all_reduce_busbw_series: NcclPoint[];
  all_reduce_gbps?: string | number;
  all_gather_gbps?: string | number;
  alltoall_gbps?: string | number;
  // parser.sh emits these via nccl_busbw_ext_csv() (llmb sweeps run all five collectives).
  reduce_scatter_gbps?: string | number;
  sendrecv_gbps?: string | number;
  /** Per-collective bus_bw series (parser.sh nccl_series_by_collective_append). */
  busbw_series_by_collective?: Record<string, NcclPoint[]>;
  oob_ar_ag_aa?: string;
  /**
   * Sweep-block counts from parser.sh nccl_sweep_counts(). ``started > concluded`` means blocks
   * are missing, which is only "still running" while the job is queued — hence sweep_running.
   * A finished job with a missing marker lost it to interleaved stdout, not to being incomplete.
   */
  sweep_blocks_started?: number;
  sweep_blocks_concluded?: number;
  sweep_blocks_missing?: number;
  sweep_complete?: boolean;
  sweep_running?: boolean;
}

interface StepPoint {
  iteration: number;
  step_time: number;
}

interface NemotronRow {
  job_id: string;
  /** Rack id for NEMO 1Rack logs; for sn-llama LLAMA rows this is the CT hostname parsed from the Slurm log basename. */
  rack: string;
  model: string;
  median_step_time: number;
  step_time_series: StepPoint[];
  /** Parser LLM window: OK / FAIL / UNK */
  status?: string;
  /** Mean step time (s) in parser LLM stats row */
  llm_step_s?: string;
  tflops_per_gpu?: string;
  mfu_percent?: string;
}

interface ParserData {
  nccl_peak_out_of_place_busbw_gbps?: NcclRow[];
  nemotron_5h_56b_train_window?: NemotronRow[];
  /** Extra LLAMA rows from ``log_dir/sn-llama/*.txt`` (same rack), merged server-side */
  sn_llama_nemotron?: NemotronRow[];
  parser_flags?: { flag: string; value: string }[];
}

type RawNemotron = Record<string, unknown> & { step_series?: unknown[]; step_time_series?: unknown[] };

type RawNccl = Record<string, unknown> & {
  all_reduce_busbw_series?: NcclPoint[];
  all_reduce_gbps?: string | number;
};

function normalizeNcclRow(row: RawNccl): NcclRow {
  const series = (Array.isArray(row.all_reduce_busbw_series) ? row.all_reduce_busbw_series : []).map((p) => ({
    size_bytes: Number((p as NcclPoint).size_bytes ?? 0),
    bus_bw_gbps: Number((p as NcclPoint).bus_bw_gbps ?? 0),
  }));
  let peak = Number(row.peak_busbw_gbps);
  if (!Number.isFinite(peak) && series.length > 0) {
    peak = Math.max(...series.map((p) => p.bus_bw_gbps));
  }
  if (!Number.isFinite(peak)) {
    const ar = row.all_reduce_gbps;
    peak = typeof ar === "number" ? ar : parseFloat(String(ar ?? ""));
  }
  if (!Number.isFinite(peak)) peak = 0;
  return {
    job_id: String(row.job_id ?? ""),
    rack: String(row.rack ?? ""),
    peak_busbw_gbps: peak,
    all_reduce_busbw_series: series,
    all_reduce_gbps: row.all_reduce_gbps as string | number | undefined,
    all_gather_gbps: row.all_gather_gbps as string | number | undefined,
    alltoall_gbps: row.alltoall_gbps as string | number | undefined,
    reduce_scatter_gbps: row.reduce_scatter_gbps as string | number | undefined,
    sendrecv_gbps: row.sendrecv_gbps as string | number | undefined,
    busbw_series_by_collective:
      row.busbw_series_by_collective && typeof row.busbw_series_by_collective === "object"
        ? (row.busbw_series_by_collective as Record<string, NcclPoint[]>)
        : undefined,
    oob_ar_ag_aa: row.oob_ar_ag_aa != null ? String(row.oob_ar_ag_aa) : undefined,
    sweep_blocks_started: Number.isFinite(Number(row.sweep_blocks_started))
      ? Number(row.sweep_blocks_started)
      : undefined,
    sweep_blocks_concluded: Number.isFinite(Number(row.sweep_blocks_concluded))
      ? Number(row.sweep_blocks_concluded)
      : undefined,
    sweep_blocks_missing: Number.isFinite(Number(row.sweep_blocks_missing))
      ? Number(row.sweep_blocks_missing)
      : undefined,
    // Older parsers (and non-nccl-tests logs) omit the counts entirely; absent means "no reason to
    // doubt it", so default to complete rather than warning on every log.
    sweep_complete: row.sweep_complete == null ? true : Boolean(row.sweep_complete),
    sweep_running: Boolean(row.sweep_running),
  };
}

/** Rows whose sweep was still being written when the log was parsed. */
function partialNcclRows(rows: NcclRow[] | undefined): NcclRow[] {
  return (rows ?? []).filter((r) => r.sweep_complete === false);
}

/**
 * Rows from a FINISHED job that are missing a conclusion marker. Not a partial read: the job has
 * left the queue, so re-parsing will never add the marker. At 576 nodes the collectives share one
 * stdout and a marker can be lost to interleaving, so the numbers are real and worth showing.
 */
function markerGapNcclRows(rows: NcclRow[] | undefined): NcclRow[] {
  return (rows ?? []).filter(
    (r) => r.sweep_complete !== false && !r.sweep_running && (r.sweep_blocks_missing ?? 0) > 0,
  );
}

function initChartsTzFromStorage(): string {
  try {
    return String(localStorage.getItem(DISPLAY_TZ_KEY) ?? "").trim();
  } catch {
    return "";
  }
}

function normalizeNemotronRow(row: RawNemotron): NemotronRow {
  const rawSteps = row.step_time_series ?? row.step_series ?? [];
  const steps = (Array.isArray(rawSteps) ? rawSteps : []).map((p) => {
    const o = p as { iteration?: number; step?: number; step_time?: number };
    const it = o.iteration ?? o.step;
    return {
      iteration: Number(it ?? 0),
      step_time: Number(o.step_time ?? 0),
    };
  });
  let median = Number(row.median_step_time);
  if (!Number.isFinite(median)) {
    median = parseFloat(String(row.step_s ?? ""));
  }
  if (!Number.isFinite(median)) median = 0;
  const model =
    typeof row.model === "string" && row.model.trim()
      ? row.model
      : `NEMO (${String(row.status ?? "LLM")})`;
  return {
    job_id: String(row.job_id ?? ""),
    rack: String(row.rack ?? ""),
    model,
    median_step_time: median,
    step_time_series: steps,
    status: row.status != null && String(row.status).trim() !== "" ? String(row.status) : undefined,
    llm_step_s: row.step_s != null && String(row.step_s).trim() !== "" ? String(row.step_s) : undefined,
    tflops_per_gpu:
      row.tflops_per_gpu != null && String(row.tflops_per_gpu).trim() !== ""
        ? String(row.tflops_per_gpu)
        : undefined,
    mfu_percent:
      row.mfu_percent != null && String(row.mfu_percent).trim() !== "" ? String(row.mfu_percent) : undefined,
  };
}

function normalizeParserPayload(data: ParserData | undefined | null): ParserData {
  if (!data) return {};
  const nccl = (data.nccl_peak_out_of_place_busbw_gbps ?? []) as unknown as RawNccl[];
  const nemo = (data.nemotron_5h_56b_train_window ?? []) as unknown as RawNemotron[];
  const snLlama = (data.sn_llama_nemotron ?? []) as unknown as RawNemotron[];
  return {
    ...data,
    nccl_peak_out_of_place_busbw_gbps: nccl.map(normalizeNcclRow),
    nemotron_5h_56b_train_window: nemo.map(normalizeNemotronRow),
    sn_llama_nemotron: snLlama.map(normalizeNemotronRow),
  };
}

interface ParserResponse {
  ok: boolean;
  rack: string;
  log_file: string;
  /**
   * Which log fed each validation stage, when resolved by rack. NCCL and NeMo run as separate
   * Slurm jobs, so each stage's numbers come from its own log; a missing key means that stage
   * has no log for this rack yet. Absent entirely for explicit path/log_file requests.
   */
  stage_logs?: Partial<Record<"nccl" | "nemo", string>>;
  job_id?: string;
  full_rack?: boolean;
  full_rack_nodes?: number | null;
  data: ParserData;
  error?: string;
  returncode?: number;
  command_display?: string;
  stderr?: string;
  grafana_uri?: string;
  grafana_port?: string | number;
  grafana_embed?: GrafanaEmbedPayload | null;
  parser_display_timezone?: string;
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  if (bytes >= 1073741824) return `${(bytes / 1073741824).toFixed(0)} GB`;
  if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(0)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${Math.round(bytes)} B`;
}

const LLAMA_SN_CHART_COLORS = [
  "#e879f9",
  "#c084fc",
  "#a78bfa",
  "#818cf8",
  "#38bdf8",
  "#2dd4bf",
  "#4ade80",
  "#facc15",
  "#fb923c",
  "#f472b6",
];

function LlamaReportTable({ rows }: { rows: NemotronRow[] }) {
  // One row per sn-llama log for the rack; a long campaign leaves plenty behind. Compact pager:
  // this lives inside a dialog, where a page-size selector would crowd the row.
  const page = usePagedRows(rows, 10);
  if (!rows.length) return null;
  return (
    <TableContainer component={Paper} variant="outlined" sx={{ mt: 1 }}>
      <Table size="small" stickyHeader>
        <TableHead>
          <TableRow>
            <TableCell>CT</TableCell>
            <TableCell>Job</TableCell>
            <TableCell>Status</TableCell>
            <TableCell>Step (s)</TableCell>
            <TableCell>TFLOP/s/GPU</TableCell>
            <TableCell>MFU%</TableCell>
            <TableCell>Model</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {page.visible.map((r, i) => (
            <TableRow key={`${r.rack}-${r.job_id}-${i}`}>
              <TableCell>{r.rack}</TableCell>
              <TableCell sx={{ fontFamily: "monospace" }}>{r.job_id}</TableCell>
              <TableCell>{r.status ?? "—"}</TableCell>
              <TableCell>{r.llm_step_s ?? (r.median_step_time > 0 ? String(r.median_step_time) : "—")}</TableCell>
              <TableCell>{r.tflops_per_gpu ?? "—"}</TableCell>
              <TableCell>{r.mfu_percent ?? "—"}</TableCell>
              <TableCell sx={{ fontSize: 12 }}>{r.model}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <TablePager {...page} noun="logs" compact />
    </TableContainer>
  );
}

/** One chart: one line series per sn-llama log row (parser step_time_series). */
function LlamaSnStepTimeMultiChart({ rows }: { rows: NemotronRow[] }) {
  if (!rows.length) return null;
  const datasets = rows
    .map((row, idx) => {
      const series = row.step_time_series || [];
      const pts = series
        .map((p) => ({ x: p.iteration, y: p.step_time }))
        .filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y));
      if (!pts.length) return null;
      const col = LLAMA_SN_CHART_COLORS[idx % LLAMA_SN_CHART_COLORS.length];
      const ctLabel = (row.rack || "").trim() || "?";
      return {
        label: `${ctLabel} · job ${row.job_id}`,
        data: pts,
        borderColor: col,
        backgroundColor: "transparent",
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 4,
        fill: false,
        tension: 0.12,
      };
    })
    .filter((d): d is NonNullable<typeof d> => d != null);
  if (!datasets.length) {
    return (
      <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
        sn-llama logs matched this rack but no step-time series were parsed (logs may still be running or use a different format).
      </Text>
    );
  }
  return (
    <Card style={{ padding: 16 }}>
      <Text kind="body/bold/md" style={{ display: "block", marginBottom: 8 }}>
        LLAMA step time (all matching logs under sn-llama/)
      </Text>
      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 12, display: "block" }}>
        {datasets.length} series — one per Slurm job / log file (CT hostname from the log name). Iterations may differ between runs.
      </Text>
      <div style={{ height: 360 }}>
        <Line
          data={{ datasets }}
          options={{
            responsive: true,
            maintainAspectRatio: false,
            parsing: { xAxisKey: "x", yAxisKey: "y" },
            plugins: {
              legend: { display: true, position: "bottom" },
              tooltip: {
                callbacks: {
                  label: (ctx) =>
                    `${ctx.dataset.label || ""}: iter ${(ctx.parsed as { x?: number }).x}: ${(ctx.parsed.y ?? 0).toFixed(3)}s`,
                },
              },
            },
            scales: {
              x: {
                type: "linear",
                title: { display: true, text: "Iteration" },
                ticks: { maxTicksLimit: 24 },
              },
              y: {
                title: { display: true, text: "Step time (s)" },
                beginAtZero: true,
              },
            },
          }}
        />
      </div>
    </Card>
  );
}

function NcclPeakTable({ rows }: { rows: NcclRow[] }) {
  if (!rows.length) {
    return <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>(no NCCL rows)</Text>;
  }
  return (
    <TableContainer component={Paper} variant="outlined" sx={{ mb: 2 }}>
      <Table size="small" stickyHeader>
        <TableHead>
          <TableRow>
            <TableCell>Rack</TableCell>
            <TableCell>Job</TableCell>
            <TableCell>all_reduce</TableCell>
            <TableCell>all_gather</TableCell>
            <TableCell>alltoall</TableCell>
            <TableCell>reduce_scatter</TableCell>
            <TableCell>sendrecv</TableCell>
            <TableCell>OOB (AR,AG,AA)</TableCell>
            <TableCell>Peak bus_bw</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {rows.map((r, i) => (
            <TableRow key={`${r.rack}-${r.job_id}-${i}`}>
              <TableCell>{r.rack}</TableCell>
              <TableCell sx={{ fontFamily: "monospace" }}>{r.job_id}</TableCell>
              <TableCell>{r.all_reduce_gbps ?? "—"}</TableCell>
              <TableCell>{r.all_gather_gbps ?? "—"}</TableCell>
              <TableCell>{r.alltoall_gbps ?? "—"}</TableCell>
              <TableCell>{r.reduce_scatter_gbps ?? "—"}</TableCell>
              <TableCell>{r.sendrecv_gbps ?? "—"}</TableCell>
              <TableCell sx={{ fontSize: 12 }}>{r.oob_ar_ag_aa ?? "—"}</TableCell>
              <TableCell>{r.peak_busbw_gbps > 0 ? `${r.peak_busbw_gbps} GB/s` : "—"}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

function formatNcclSizeAxisTick(tickValue: number | string): string {
  const v = typeof tickValue === "number" ? tickValue : Number(tickValue);
  if (!Number.isFinite(v) || v <= 0) return "";
  if (v >= 1e12) return `${(v / 1e12).toFixed(1)}T`;
  if (v >= 1e9) return `${(v / 1e9).toFixed(1)}G`;
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(0)}k`;
  return String(Math.round(v));
}

/** NCCL all_reduce: message size (bytes) on logarithmic x, bus_bw linear y (same convention as Analytics). */
function NcclLog2LineCharts({ rows }: { rows: NcclRow[] }) {
  if (!rows.length) return null;
  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 3 }}>
      {rows.map((row, idx) => {
        const toPts = (arr: NcclPoint[]) =>
          (arr || [])
            .map((p) => ({ x: p.size_bytes, y: p.bus_bw_gbps }))
            .filter((p) => Number.isFinite(p.x) && p.x > 0 && Number.isFinite(p.y))
            .sort((a, b) => a.x - b.x);
        // One line per collective when the parser supplied them (llmb sweeps run five);
        // otherwise fall back to the legacy all_reduce-only series so older logs still chart.
        const byColl = row.busbw_series_by_collective || {};
        const collNames = Object.keys(byColl)
          .filter((c) => (byColl[c] || []).length > 0)
          .sort();
        const collPts: { name: string; pts: { x: number; y: number }[] }[] = collNames.length
          ? collNames.map((c) => ({ name: c, pts: toPts(byColl[c]) }))
          : [{ name: "all_reduce", pts: toPts(row.all_reduce_busbw_series || []) }];
        const pts = collPts.flatMap((c) => c.pts);
        if (!pts.length) {
          if (row.peak_busbw_gbps > 0) {
            return (
              <Card key={idx} style={{ padding: 16 }}>
                <Flex gap="2" align="center" style={{ marginBottom: 8 }} wrap="wrap">
                  <Text kind="body/bold/md">NCCL all_reduce (peak only)</Text>
                  <Tag color="green" kind="solid" readOnly density="compact">
                    Peak: {row.peak_busbw_gbps} GB/s
                  </Tag>
                </Flex>
                <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                  No per-message-size bus_bw series in this log (missing or unparsed all_reduce_perf_mpi block).
                </Text>
              </Card>
            );
          }
          return null;
        }
        return (
          <Card key={idx} style={{ padding: 16 }}>
            <Flex gap="2" align="center" style={{ marginBottom: 8 }} wrap="wrap">
              <Text kind="body/bold/md">NCCL out-of-place bus_bw vs message size — {collPts.length > 1 ? `${collPts.length} collectives` : collPts[0]?.name}</Text>
              <Tag color="green" kind="solid" readOnly density="compact">
                Peak: {row.peak_busbw_gbps} GB/s
              </Tag>
            </Flex>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 8, display: "block" }}>
              {pts.length} points across {collPts.length} collective{collPts.length === 1 ? "" : "s"} — x-axis message size (bytes, log scale), y-axis bus_bw (GB/s); job {row.job_id}. Best-performing sweep block per collective.
            </Text>
            <div style={{ height: 280 }}>
              <Line
                data={{
                  datasets: collPts.map((c, ci) => {
                    const palette = [
                      "#ffb454", // all_reduce
                      "#76b900", // NVIDIA green
                      "#4fc3f7",
                      "#ba68c8",
                      "#ef5350",
                      "#26a69a",
                    ];
                    const color = palette[ci % palette.length];
                    return {
                      label: c.name,
                      data: c.pts.map((p) => ({ x: p.x, y: p.y })),
                      borderColor: color,
                      backgroundColor: color,
                      borderWidth: 2,
                      pointRadius: 2,
                      pointHoverRadius: 5,
                      tension: 0.1,
                      fill: false,
                    };
                  }),
                }}
                options={{
                  responsive: true,
                  maintainAspectRatio: false,
                  parsing: false,
                  plugins: {
                    legend: { display: true, position: "bottom" },
                    tooltip: {
                      callbacks: {
                        title: (items) => {
                          const rawPt = items[0]?.raw as { x?: number } | undefined;
                          const sz = rawPt?.x;
                          if (sz == null || !Number.isFinite(sz)) return "";
                          return `size: ${formatBytes(sz)}`;
                        },
                        label: (ctx) => `${ctx.dataset.label}: ${(ctx.parsed.y ?? 0).toFixed(2)} GB/s`,
                      },
                    },
                  },
                  scales: {
                    x: {
                      type: "logarithmic",
                      suggestedMin: 1,
                      title: { display: true, text: "Message size (bytes, log scale)" },
                      ticks: {
                        callback: (v: number | string) => formatNcclSizeAxisTick(v),
                      },
                    },
                    y: {
                      title: { display: true, text: "bus_bw (GB/s)" },
                      beginAtZero: true,
                    },
                  },
                }}
              />
            </div>
          </Card>
        );
      })}
    </Box>
  );
}

function NcclReviewSection({ rows }: { rows: NcclRow[] }) {
  if (!rows.length) {
    return <Text kind="body/regular/md">No NCCL data available</Text>;
  }
  return (
    <Box>
      <Text kind="body/bold/md" style={{ display: "block", marginBottom: 8 }}>
        NCCL peak OOB busbw (GB/s)
      </Text>
      <NcclPeakTable rows={rows} />
      <NcclLog2LineCharts rows={rows} />
    </Box>
  );
}

function StepTimeChart({ rows }: { rows: NemotronRow[] }) {
  if (!rows.length) return <Text kind="body/regular/md">No step time data available</Text>;

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 3 }}>
      {rows.map((row, idx) => {
        const series = row.step_time_series || [];
        if (!series.length) return null;

        const pts = series
          .map((p) => ({ x: p.iteration, y: p.step_time }))
          .filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y));

        if (!pts.length) return null;

        return (
          <Card key={idx} style={{ padding: 16 }}>
            <Flex gap="2" align="center" style={{ marginBottom: 12 }}>
              <Text kind="body/bold/md">{row.model} — Step time vs iteration</Text>
              <Tag color="teal" kind="solid" readOnly density="compact">
                Median: {row.median_step_time}s
              </Tag>
            </Flex>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 8, display: "block" }}>
              {pts.length} points (iterations {Math.round(Math.min(...pts.map((p) => p.x)))}–
              {Math.round(Math.max(...pts.map((p) => p.x)))})
            </Text>
            <div style={{ height: 300 }}>
              <Line
                data={{
                  datasets: [
                    {
                      label: `step_time (s) — ${row.model}`,
                      data: pts,
                      borderColor: "#58d6ff",
                      backgroundColor: "rgba(88, 214, 255, 0.1)",
                      borderWidth: 2,
                      pointRadius: 1,
                      fill: true,
                      tension: 0.2,
                    },
                  ],
                }}
                options={{
                  responsive: true,
                  maintainAspectRatio: false,
                  parsing: { xAxisKey: "x", yAxisKey: "y" },
                  plugins: {
                    legend: { display: false },
                    tooltip: {
                      callbacks: {
                        label: (ctx) => `iter ${(ctx.parsed as { x?: number }).x}: ${(ctx.parsed.y ?? 0).toFixed(3)}s`,
                      },
                    },
                  },
                  scales: {
                    x: {
                      type: "linear",
                      title: { display: true, text: "Iteration" },
                      ticks: { maxTicksLimit: 24 },
                    },
                    y: {
                      title: { display: true, text: "Step time (s)" },
                      beginAtZero: true,
                    },
                  },
                }}
              />
            </div>
          </Card>
        );
      })}
    </Box>
  );
}

function NemoReviewSection({
  rows,
  rack,
  grafanaEmbed,
  grafanaUri,
  grafanaPort,
  parserDisplayTimezone,
}: {
  rows: NemotronRow[];
  rack?: string;
  grafanaEmbed: GrafanaEmbedPayload | null | undefined;
  grafanaUri?: string;
  grafanaPort?: string | number;
  parserDisplayTimezone: string;
}) {
  return (
    <Box>
      <StepTimeChart rows={rows} />
      <ParserGrafanaEmbeds
        grafanaEmbed={grafanaEmbed}
        grafanaUri={grafanaUri}
        grafanaPort={grafanaPort}
        parserDisplayTimezone={parserDisplayTimezone}
        rack={rack}
      />
    </Box>
  );
}

function LlamaReviewSection({ rows }: { rows: NemotronRow[] }) {
  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <LlamaSnStepTimeMultiChart rows={rows} />
      <Card style={{ padding: 16 }}>
        <Text kind="body/bold/md" style={{ display: "block", marginBottom: 8 }}>
          LLAMA metrics (sn-llama/)
        </Text>
        <LlamaReportTable rows={rows} />
      </Card>
    </Box>
  );
}

function SummaryTab({
  data,
  rack,
  logFile,
  stageLogs,
  commandDisplay,
  returncode,
  stderr,
}: {
  data: ParserData;
  rack: string;
  logFile: string;
  stageLogs?: Partial<Record<"nccl" | "nemo", string>>;
  commandDisplay?: string;
  returncode?: number;
  stderr?: string;
}) {
  const ncclRows = data.nccl_peak_out_of_place_busbw_gbps || [];
  const nemoRows = data.nemotron_5h_56b_train_window || [];
  const snLlamaRows = data.sn_llama_nemotron || [];
  const flags = data.parser_flags || [];
  const logIsLlama = /llama3_8b|nemo-llama/i.test((logFile || "").toLowerCase());
  const showLlamaFromPrimary = snLlamaRows.length === 0 && logIsLlama && nemoRows.length > 0;

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      {commandDisplay && (
        <Card style={{ padding: 12 }}>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block", marginBottom: 4 }}>
            Command
          </Text>
          <Text kind="mono/sm" style={{ wordBreak: "break-all" }}>{commandDisplay}</Text>
        </Card>
      )}
      {logFile && (
        <Card style={{ padding: 12 }}>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block", marginBottom: 4 }}>
            Log path
          </Text>
          <Text kind="mono/sm" style={{ wordBreak: "break-all" }}>{logFile}</Text>
        </Card>
      )}
      {/* Two stages, two jobs, two logs: name the source of each tab so a stage that has not
          run yet reads as missing rather than as a bad result. */}
      {stageLogs && Object.keys(stageLogs).length > 1 && (
        <Card style={{ padding: 12 }}>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block", marginBottom: 4 }}>
            Stage logs
          </Text>
          {(["nccl", "nemo"] as const).map((stage) =>
            stageLogs[stage] ? (
              <div key={stage} style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
                <Text kind="body/regular/sm" style={{ minWidth: 44, color: "var(--text-color-secondary)" }}>
                  {stage === "nccl" ? "NCCL" : "NEMO"}
                </Text>
                <Text kind="mono/sm" style={{ wordBreak: "break-all" }}>
                  {stageLogs[stage]!.split("/").pop()}
                </Text>
              </div>
            ) : null,
          )}
        </Card>
      )}
      {returncode != null && returncode !== 0 && (
        <Banner status="warning">parser.sh exit code {returncode}</Banner>
      )}
      {stderr && (
        <Card style={{ padding: 12, backgroundColor: "var(--background-color-surface-raised, #2a2418)" }}>
          <Text kind="body/bold/sm" style={{ marginBottom: 4 }}>stderr</Text>
          <pre style={{ margin: 0, fontSize: 12, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{stderr}</pre>
        </Card>
      )}
      <Card style={{ padding: 16 }}>
        <Text kind="body/bold/md" style={{ display: "block", marginBottom: 8 }}>Overview</Text>
        <Flex gap="3" wrap="wrap">
          <div>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block" }}>Rack</Text>
            <Text kind="body/semibold/md">{rack}</Text>
          </div>
          <div>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block" }}>Log file</Text>
            <Text kind="mono/sm">{logFile.split("/").pop()}</Text>
          </div>
          {ncclRows.length > 0 && (
            <div>
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block" }}>NCCL peak bus_bw</Text>
              <Text kind="body/semibold/md">{ncclRows[0].peak_busbw_gbps} GB/s</Text>
            </div>
          )}
          {nemoRows.length > 0 && (
            <div>
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block" }}>Median step time</Text>
              <Text kind="body/semibold/md">{nemoRows[0].median_step_time}s</Text>
            </div>
          )}
        </Flex>
      </Card>

      {flags.length > 0 && (
        <Card style={{ padding: 16 }}>
          <Text kind="body/bold/md" style={{ display: "block", marginBottom: 8 }}>Parser flags</Text>
          <Flex gap="2" wrap="wrap">
            {flags.map((f, i) => (
              <Tag key={i} color="gray" kind="outline" readOnly density="compact">
                {f.flag}: {f.value}
              </Tag>
            ))}
          </Flex>
        </Card>
      )}

      {snLlamaRows.length > 0 && (
        <Card style={{ padding: 16, borderLeft: "4px solid #c084fc" }}>
          <Text kind="body/bold/md" style={{ display: "block", marginBottom: 4 }}>
            LLAMA report (sn-llama/)
          </Text>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 8, display: "block" }}>
            {snLlamaRows.length} log file{snLlamaRows.length === 1 ? "" : "s"} under <span style={{ fontFamily: "monospace" }}>sn-llama/</span> for this rack. See the LLAMA tab for combined step-time chart.
          </Text>
          <LlamaReportTable rows={snLlamaRows} />
        </Card>
      )}

      {showLlamaFromPrimary && (
        <Card style={{ padding: 16, borderLeft: "4px solid #c084fc" }}>
          <Text kind="body/bold/md" style={{ display: "block", marginBottom: 4 }}>
            LLAMA report (selected log)
          </Text>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 8, display: "block" }}>
            This log is a LLAMA run; no additional sn-llama/ files were merged for this rack.
          </Text>
          <LlamaReportTable rows={nemoRows} />
        </Card>
      )}
    </Box>
  );
}

export function ParserResultsDialog({
  open,
  onClose,
  rack,
  jobId,
  logDir,
  logFileBasename,
  logPath,
  initialTabIndex = 0,
}: ParserResultsDialogProps) {
  const [loading, setLoading] = useState(false);
  /** Bumped by the Refresh button and by the partial-sweep timer to re-run parser.sh. */
  const [refreshNonce, setRefreshNonce] = useState(0);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const [error, setError] = useState("");
  const [result, setResult] = useState<ParserResponse | null>(null);
  const [tabIndex, setTabIndex] = useState(0);
  const [chartsTz, setChartsTz] = useState(initChartsTzFromStorage);
  const [fullRack, setFullRack] = useState(initFullRackFromStorage);
  const [nvloomPath, setNvloomPath] = useState("");
  const [nvloomContent, setNvloomContent] = useState("");
  const [nvloomLoading, setNvloomLoading] = useState(false);
  const [nvloomError, setNvloomError] = useState("");
  const [clusterkitRuns, setClusterkitRuns] = useState<ClusterkitRunRow[]>([]);
  const [clusterkitListLoading, setClusterkitListLoading] = useState(false);
  const [clusterkitListError, setClusterkitListError] = useState("");

  /** Explicit path/basename skips rack resolution — full-rack filter does not apply. */
  const rackResolveMode = !(logPath ?? "").trim() && !(logFileBasename ?? "").trim();

  useEffect(() => {
    if (!open) return;
    setChartsTz(initChartsTzFromStorage());
    setFullRack(initFullRackFromStorage());
    setTabIndex(Math.min(Math.max(0, initialTabIndex), 10));
  }, [open, initialTabIndex]);

  const persistChartsTz = (value: string) => {
    setChartsTz(value);
    try {
      if (!value) localStorage.removeItem(DISPLAY_TZ_KEY);
      else localStorage.setItem(DISPLAY_TZ_KEY, value);
    } catch {
      /* ignore */
    }
  };

  const persistFullRack = (checked: boolean) => {
    setFullRack(checked);
    try {
      localStorage.setItem(FULL_RACK_KEY, checked ? "1" : "0");
    } catch {
      /* ignore */
    }
  };

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError("");
    setResult(null);
    setFetchedAt(null);

    const params = new URLSearchParams();
    if (logDir) params.set("log_dir", logDir);
    const lp = (logPath ?? "").trim();
    const lf = (logFileBasename ?? "").trim();
    if (lp) {
      params.set("path", lp);
      if (rack.trim()) params.set("rack", rack.trim());
    } else if (lf) {
      params.set("log_file", lf);
      if (rack.trim()) params.set("rack", rack.trim());
    } else {
      if (rack) params.set("rack", rack);
      if (jobId && jobId !== "-") params.set("job_id", jobId);
      if (fullRack) params.set("full_rack", "1");
    }
    const tzApi = chartsTzForApi(chartsTz);
    if (tzApi) params.set("display_timezone", tzApi);

    apiFetch(`/api/parser-results?${params}`)
      .then((res) => res.json())
      .then((json: ParserResponse & { ok?: boolean; error?: string }) => {
        if (json.ok) {
          const data = normalizeParserPayload(json.data);
          setResult({ ...json, data });
          setFetchedAt(new Date());
          const nvloomIdx = (data.sn_llama_nemotron?.length ?? 0) > 0 ? 4 : 3;
          setTabIndex((t) => Math.min(t, nvloomIdx));
        } else {
          setError(json.error || "Parser failed");
        }
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
    // refreshNonce re-runs this effect: parser.sh is re-executed against the log, which is the
    // only way to pick up rows written since the dialog opened.
  }, [open, rack, jobId, logDir, logFileBasename, logPath, chartsTz, fullRack, refreshNonce]);

  // While a sweep is still being written, re-parse on a timer so the chart fills in instead of
  // sitting on a partial read until the operator notices. Stops as soon as it is complete.
  const partialRows = partialNcclRows(result?.data?.nccl_peak_out_of_place_busbw_gbps);
  const sweepPartial = partialRows.length > 0;
  const markerGapRows = markerGapNcclRows(result?.data?.nccl_peak_out_of_place_busbw_gbps);
  useEffect(() => {
    if (!open || !sweepPartial || loading) return;
    const t = setTimeout(() => setRefreshNonce((n) => n + 1), PARTIAL_REPARSE_MS);
    return () => clearTimeout(t);
  }, [open, sweepPartial, loading, refreshNonce]);

  useEffect(() => {
    if (!open || !(logDir || "").trim() || !rack.trim() || !result) {
      setNvloomPath("");
      setNvloomContent("");
      setNvloomLoading(false);
      setNvloomError("");
      return;
    }
    const ac = new AbortController();
    setNvloomLoading(true);
    setNvloomError("");
    const params = new URLSearchParams();
    params.set("log_dir", logDir!.trim());
    params.set("rack", rack.trim());
    apiFetch(`/api/nvloom-heatmaps-log?${params}`, { signal: ac.signal })
      .then(async (res) => {
        const j = (await res.json()) as { ok?: boolean; error?: string; path?: string; content?: string };
        if (!res.ok) {
          setNvloomPath("");
          setNvloomContent("");
          if (res.status === 404) {
            setNvloomError("");
            return;
          }
          setNvloomError(j.error || res.statusText || "NVLOOM log request failed");
          return;
        }
        if (j.ok) {
          setNvloomPath(String(j.path ?? ""));
          setNvloomContent(typeof j.content === "string" ? j.content : "");
          setNvloomError("");
        } else {
          setNvloomPath("");
          setNvloomContent("");
          setNvloomError(j.error || "NVLOOM log unavailable");
        }
      })
      .catch((err: Error) => {
        if (err.name === "AbortError") return;
        setNvloomPath("");
        setNvloomContent("");
        setNvloomError(err.message);
      })
      .finally(() => {
        if (!ac.signal.aborted) setNvloomLoading(false);
      });
    return () => ac.abort();
  }, [open, logDir, rack, result]);

  useEffect(() => {
    if (!open || !(logDir || "").trim() || !rack.trim() || !result) {
      setClusterkitRuns([]);
      setClusterkitListLoading(false);
      setClusterkitListError("");
      return;
    }
    const ac = new AbortController();
    setClusterkitRuns([]);
    setClusterkitListLoading(true);
    setClusterkitListError("");
    const params = new URLSearchParams();
    params.set("log_dir", logDir!.trim());
    params.set("rack", rack.trim());
    apiFetch(`/api/clusterkit-results?${params}`, { signal: ac.signal })
      .then(async (res) => {
        const j = (await res.json()) as { ok?: boolean; error?: string; runs?: ClusterkitRunRow[] };
        if (!res.ok || !j.ok) {
          setClusterkitRuns([]);
          setClusterkitListError(j.error || res.statusText || "ClusterKit list failed");
          return;
        }
        setClusterkitRuns(Array.isArray(j.runs) ? j.runs : []);
        setClusterkitListError("");
      })
      .catch((err: Error) => {
        if (err.name === "AbortError") return;
        setClusterkitRuns([]);
        setClusterkitListError(err.message);
      })
      .finally(() => {
        if (!ac.signal.aborted) setClusterkitListLoading(false);
      });
    return () => ac.abort();
  }, [open, logDir, rack, result]);

  const ncclRows = result?.data?.nccl_peak_out_of_place_busbw_gbps || [];
  const nemoRows = result?.data?.nemotron_5h_56b_train_window || [];
  const snLlamaRows = result?.data?.sn_llama_nemotron || [];
  const showLlamaTab = snLlamaRows.length > 0;
  const nvloomTabIndex = showLlamaTab ? 4 : 3;
  const showClusterkitTab = clusterkitRuns.length > 0;
  const clusterkitTabIndex = showClusterkitTab ? nvloomTabIndex + 1 : -1;
  const maxTabIdx = showClusterkitTab ? nvloomTabIndex + 1 : nvloomTabIndex;
  const effectiveTab = Math.min(tabIndex, maxTabIdx);

  useEffect(() => {
    setTabIndex((t) => Math.min(t, maxTabIdx));
  }, [maxTabIdx]);
  const pdisp = String(result?.parser_display_timezone ?? "").trim() || "utc";
  const displayJobId =
    (result?.job_id && result.job_id !== "-" ? result.job_id : "") ||
    (jobId && jobId !== "-" ? jobId : "");
  const nodesLabel = result?.full_rack_nodes ?? 18;

  return (
    <Dialog open={open} onClose={onClose} maxWidth="lg" fullWidth aria-label="Parser results: NCCL and NEMO benchmark charts">
      <DialogTitle sx={{ fontWeight: 600 }}>
        <Box sx={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 2, flexWrap: "wrap" }}>
          <span>
            Parser results — {rack}
            {displayJobId ? ` (job ${displayJobId})` : ""}
            {result?.full_rack ? ` · full rack (${nodesLabel}N)` : ""}
          </span>
          <Flex align="center" gap="3" wrap="wrap">
            <Button
              kind="secondary"
              size="small"
              disabled={loading}
              onClick={() => setRefreshNonce((n) => n + 1)}
            >
              <RefreshCw size={14} style={{ marginRight: 6 }} />
              {loading ? "Parsing…" : "Refresh"}
            </Button>
            {fetchedAt && (
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-subtle)" }}>
                {`parsed ${fetchedAt.toLocaleTimeString()}`}
              </Text>
            )}
            {rackResolveMode && (
              <Checkbox
                checked={fullRack}
                disabled={loading}
                onCheckedChange={(v) => persistFullRack(v === true)}
                slotLabel={
                  <Text kind="body/regular/sm">Full rack validation ({nodesLabel}N)</Text>
                }
              />
            )}
            <FormControl size="small" sx={{ minWidth: 200 }}>
              <InputLabel id="parser-charts-tz-label">Charts TZ</InputLabel>
              <Select
                labelId="parser-charts-tz-label"
                label="Charts TZ"
                value={chartsTz || ""}
                onChange={(e) => persistChartsTz(String(e.target.value))}
                disabled={loading}
              >
                <MenuItem value="">
                  <em>Server default</em>
                </MenuItem>
                <MenuItem value="browser">Browser local (IANA)</MenuItem>
                <MenuItem value="utc">UTC</MenuItem>
                <MenuItem value="Europe/Berlin">Europe/Berlin</MenuItem>
                <MenuItem value="America/Los_Angeles">America/Los_Angeles</MenuItem>
              </Select>
            </FormControl>
          </Flex>
        </Box>
      </DialogTitle>
      <DialogContent dividers sx={{ minWidth: 0, boxSizing: "border-box" }}>
        {loading && (
          <Box sx={{ display: "flex", justifyContent: "center", py: 6 }}>
            <CircularProgress />
          </Box>
        )}
        {error && (
          <div style={{ marginBottom: 16 }}>
            <Banner status="error">{error}</Banner>
          </div>
        )}
        {/* A sweep still being written parses to a truncated series: one data row gives a
            one-point chart whose "peak" is whatever size happened to be flushed. Say so, rather
            than presenting a partial read as this rack's result. Re-parses automatically. */}
        {sweepPartial && (
          <div style={{ marginBottom: 16 }}>
            <Banner status="warning">
              {(() => {
                const r = partialRows[0];
                const done = r?.sweep_blocks_concluded ?? 0;
                const started = r?.sweep_blocks_started ?? 0;
                const pts = r?.all_reduce_busbw_series?.length ?? 0;
                return (
                  `This sweep is still running — ${done} of ${started} block(s) finished, so the ` +
                  `${pts} point(s) below are a partial read and the peak is not the rack's result. ` +
                  `Re-checking every ${Math.round(PARTIAL_REPARSE_MS / 1000)}s; or press Refresh.`
                );
              })()}
            </Banner>
          </div>
        )}
        {/* Finished job, missing marker: the numbers stand. Say the marker is missing without
            implying the run is incomplete, and do NOT poll — re-parsing cannot add it. */}
        {!sweepPartial && markerGapRows.length > 0 && (
          <div style={{ marginBottom: 16 }}>
            <Banner status="info">
              {(() => {
                const r = markerGapRows[0];
                const done = r?.sweep_blocks_concluded ?? 0;
                const started = r?.sweep_blocks_started ?? 0;
                return (
                  `This job has finished, but only ${done} of ${started} collective(s) printed a ` +
                  `completion marker. At this node count the ranks share one stdout and a marker ` +
                  `can be lost to interleaving, so the results below are the run's own numbers.`
                );
              })()}
            </Banner>
          </div>
        )}
        {result && !loading && (
          <>
            <Tabs
              value={effectiveTab}
              onChange={(_, v) => setTabIndex(v as number)}
              sx={{ borderBottom: 1, borderColor: "divider", mb: 3 }}
            >
              <Tab label="Summary" />
              <Tab label={`NCCL (${ncclRows.length})`} />
              <Tab label={`NEMO + GPU (${nemoRows.length})`} />
              {showLlamaTab && <Tab label={`LLAMA (${snLlamaRows.length})`} />}
              <Tab label="NVLOOM" />
              {showClusterkitTab && <Tab label={`ClusterKit (${clusterkitRuns.length})`} />}
            </Tabs>
            {effectiveTab === 0 && (
              <SummaryTab
                data={result.data}
                rack={result.rack}
                logFile={result.log_file}
                stageLogs={result.stage_logs}
                commandDisplay={result.command_display}
                returncode={result.returncode}
                stderr={result.stderr}
              />
            )}
            {effectiveTab === 1 && <NcclReviewSection rows={ncclRows} />}
            {effectiveTab === 2 && (
              <NemoReviewSection
                rows={nemoRows}
                rack={rack}
                grafanaEmbed={result.grafana_embed}
                grafanaUri={result.grafana_uri}
                grafanaPort={result.grafana_port}
                parserDisplayTimezone={pdisp}
              />
            )}
            {showLlamaTab && effectiveTab === 3 && <LlamaReviewSection rows={snLlamaRows} />}
            {effectiveTab === nvloomTabIndex && (
              <NvloomHeatmapsSection
                logPath={nvloomPath}
                content={nvloomContent}
                loading={nvloomLoading}
                error={nvloomError}
              />
            )}
            {showClusterkitTab && clusterkitTabIndex >= 0 && effectiveTab === clusterkitTabIndex && logDir && (
              <ClusterkitResultsSection
                logDir={logDir.trim()}
                rack={rack.trim()}
                runs={clusterkitRuns}
                listLoading={clusterkitListLoading}
                listError={clusterkitListError}
              />
            )}
          </>
        )}
      </DialogContent>
      <DialogActions>
        <Button kind="secondary" onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}
