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
import { useState, useCallback, useMemo, useEffect } from "react";
import Box from "@mui/material/Box";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import CircularProgress from "@mui/material/CircularProgress";
import { Text, Button, Banner, Tabs, Tag, ProgressBar, Select, FormField } from "@nvidia/foundations-react-core";
import Slider from "@mui/material/Slider";
import MuiTooltip from "@mui/material/Tooltip";
import { alpha } from "@mui/material/styles";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  LogarithmicScale,
  PointElement,
  LineElement,
  BarElement,
  Title,
  Tooltip,
  Legend,
} from "chart.js";
import { Line, Bar } from "react-chartjs-2";
import { ReportGenerator } from "../components/analytics/ReportGenerator";
import { PageHeader } from "../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { NodeHeatmap } from "../components/analytics/NodeHeatmap";
import { IssueHealthOverview } from "../components/analytics/IssueHealthOverview";
import { type NodeInfo } from "../components/rack/NodeSelector";
import { parseNodesFromRack } from "../lib/parseNodesFromRack";
import { NOT_IN_INVENTORY_GROUP, UNPLACED_GROUP, fleetNodesFromInventory } from "../lib/rackNodesFromInventory";
import { getBringupFleet } from "../services/bringupService";
import { useActiveCluster } from "../context/ActiveClusterContext";
import { useSortedPagedRows } from "../hooks/useSortedPagedRows";
import { SortableHeaderCell } from "../components/SortableHeaderCell";
import { TablePager } from "../components/TablePager";
import type { BringupFleet } from "../types/bringup";
import type { RackRow, RackStatusResponse } from "../types/api";
import { apiFetch } from "../lib/apiFetch";

/** Stable empty array: a fresh `[]` per render would re-slice the pager's memo every time. */
const EMPTY_ROWS: never[] = [];

const RACK_TAB = "rack";
const NODE_TAB = "node";

ChartJS.register(
  CategoryScale,
  LinearScale,
  LogarithmicScale,
  PointElement,
  LineElement,
  BarElement,
  Title,
  Tooltip,
  Legend
);

type BusBwSeriesBlock = {
  sizes_bytes: number[];
  median_bus_bw_gbps: (number | null)[];
  per_rack: { rack: string; log_file: string; bus_bw_gbps: (number | null)[]; pct_vs_median: (number | null)[] }[];
};

/** The single-rack collectives an operator can switch between; keyed as the server names them. */
type CollectiveKey = "all_reduce" | "all_gather" | "alltoall";
const COLLECTIVE_OPTIONS: { value: `${CollectiveKey}_gbps`; children: string }[] = [
  { value: "all_reduce_gbps", children: "all_reduce_gbps" },
  { value: "all_gather_gbps", children: "all_gather_gbps" },
  { value: "alltoall_gbps", children: "alltoall_gbps" },
];

type DeviationRow = NonNullable<AnalyticsPayload["single_rack"]>["nccl_collectives_deviation_pct"][number];
type SingleRackNemotronRow = NonNullable<AnalyticsPayload["single_rack"]>["nemotron"][number];

/** Raw text of a column, for the shared sort (numbers and rack ids order naturally, blanks last). */
const text = (v: string | number | null | undefined) => (v == null ? "" : String(v));
const DEVIATION_COLUMNS: [string, "right" | undefined][] = [
  ["Metric", undefined], ["Rack", undefined], ["Log", undefined],
  ["Value (GB/s)", "right"], ["Fleet median", "right"], ["% vs median", "right"],
];
function deviationCell(r: DeviationRow, col: number): string {
  return text([r.metric, r.rack, r.log_file, r.value_gbps, r.fleet_median_gbps, r.pct_vs_median][col]);
}
const NEMOTRON_COLUMNS: [string, "right" | undefined][] = [
  ["Rack", undefined], ["Log", undefined], ["Median step (s)", "right"], ["TFLOPs/GPU", "right"],
  ["Step CV%", "right"], ["% vs mean TFLOPs", "right"], ["% vs mean step", "right"],
];
function nemotronCell(r: SingleRackNemotronRow, col: number): string {
  return text([
    r.rack, r.log_file, r.median_step_time, r.tflops_per_gpu, r.step_time_cv_pct,
    r.pct_vs_mean_tflops, r.pct_vs_mean_step_time,
  ][col]);
}

type AnalyticsPayload = {
  ok: boolean;
  error?: string;
  /** ISO-8601 UTC from server when the aggregate was produced or cached. */
  analyzed_at?: string;
  /** SHA-256 of sorted (rack,nemo) rows used for this run (server-side). */
  rows_fingerprint?: string;
  /** True when body came from ``GET /api/analytics/last`` disk cache. */
  cached?: boolean;
  single_rack?: {
    nccl_collectives: { rack: string; log_file: string; all_reduce_gbps: number | null; all_gather_gbps: number | null; alltoall_gbps: number | null; reduce_scatter_gbps?: number | null; sendrecv_gbps?: number | null }[];
    nccl_collectives_deviation_pct: {
      metric: string;
      rack: string;
      log_file: string;
      value_gbps: number;
      fleet_median_gbps: number;
      pct_vs_median: number;
    }[];
    nccl_ar_series: BusBwSeriesBlock;
    /** The same block per collective (all_reduce, all_gather, alltoall); absent from older servers. */
    nccl_series_by_collective?: Partial<Record<CollectiveKey, BusBwSeriesBlock>>;
    nemotron: {
      rack: string;
      log_file: string;
      median_step_time: number | null;
      tflops_per_gpu: number | null;
      step_time_cv_pct: number | null;
      pct_vs_mean_tflops: number | null;
      pct_vs_mean_step_time: number | null;
    }[];
  };
  multirack?: {
    nccl_series_by_log: { label: string; basename: string; series: { size_bytes: number; bus_bw_gbps: number }[] }[];
    /** NCCL peaks by the node count each run used (a fleet-scale view; absent from older servers). */
    nccl_scaling?: {
      num_nodes: number | null;
      /** GPUs the newest log in the row used (its MPI rank count); null when no log says. */
      num_gpus?: number | null;
      n_logs: number;
      n_logs_no_metrics?: number;
      logs_no_metrics?: string[];
      mean_all_reduce_gbps: number | null;
      mean_all_gather_gbps: number | null;
      mean_alltoall_gbps: number | null;
      mean_reduce_scatter_gbps: number | null;
      mean_sendrecv_gbps: number | null;
    }[];
    nemotron_scaling: {
      n_racks: number;
      n_logs: number;
      /** Successful runs in this bucket whose step/TFLOPs could not be averaged. */
      n_logs_no_metrics?: number;
      logs_no_metrics?: string[];
      mean_tflops_per_gpu: number | null;
      mean_step_s: number | null;
    }[];
    nemotron_throughput_scaling?: {
      n_racks: number;
      n_logs: number;
      n_logs_no_metrics?: number;
      logs_no_metrics?: string[];
      num_nodes: number | null;
      num_gpus: number | null;
      mean_step_s: number | null;
      mean_tflops_per_gpu: number | null;
      mean_throughput_tokens_s: number | null;
      mean_throughput_tokens_s_per_gpu: number | null;
      time_to_train_1t_days: number | null;
    }[];
  };
};

type NemotronScalingRow = NonNullable<AnalyticsPayload["multirack"]>["nemotron_scaling"][number];

/** A metric that could not be averaged reads as an em dash — never as a blank or a misleading 0. */
const metricOrDash = (v: number | null | undefined) => (v == null ? "—" : v);

/**
 * GPUs a scale row used: the log's own rank count when it states one; otherwise nodes x the
 * platform's GPUs per node, marked with "≈" and a tooltip so a design figure never reads as measured.
 */
function GpuCountCell({ measured, nodes, gpusPerNode }: { measured?: number | null; nodes: number | null; gpusPerNode: number }) {
  if (measured) return <TableCell align="right">{measured}</TableCell>;
  if (nodes && gpusPerNode > 0) {
    return (
      <TableCell align="right" title={`Not stated in the log: ${nodes} nodes x ${gpusPerNode} GPUs per node (platform design)`}>
        ≈{nodes * gpusPerNode}
      </TableCell>
    );
  }
  return <TableCell align="right">—</TableCell>;
}

/** Log count that also reports successful runs which yielded no averageable metrics. */
function LogCountCell({ nLogs, nNoMetrics }: { nLogs: number; nNoMetrics?: number }) {
  const missing = nNoMetrics ?? 0;
  return (
    <TableCell align="right">
      {nLogs}
      {missing > 0 ? (
        <Tag color="yellow" kind="outline" density="compact" readOnly style={{ marginLeft: 8 }}>
          {`+${missing} no metrics`}
        </Tag>
      ) : null}
    </TableCell>
  );
}

const CHART_COLORS = [
  "#76B900",
  "#2196f3",
  "#ff9800",
  "#e91e63",
  "#9c27b0",
  "#00bcd4",
  "#cddc39",
  "#795548",
];

const nemotronPctBarScales = {
  x: { title: { display: true, text: "Rack" } },
  y: { title: { display: true, text: "% vs fleet mean" } },
};

const nemotronPctBarOptionsTflops = {
  responsive: true,
  maintainAspectRatio: false,
  scales: nemotronPctBarScales,
  plugins: {
    legend: { display: false },
    title: { display: true, text: "% vs mean TFLOPs/GPU" },
  },
};

const nemotronPctBarOptionsStepTime = {
  responsive: true,
  maintainAspectRatio: false,
  scales: nemotronPctBarScales,
  plugins: {
    legend: { display: false },
    title: { display: true, text: "% vs mean step time" },
  },
};

interface AnalyticsViewProps {
  logDir: string;
  rows: RackRow[];
  /**
   * Full status payload, for the node-level view. Optional so the rack-level analytics keep
   * working without it — the heatmap needs the naming templates and BMC map that only the
   * whole response carries.
   */
  status?: RackStatusResponse | null;
}

/** How often to ask the server how far through the log corpus it is. */
const ANALYTICS_POLL_MS = 1000;

/** Live position of a running aggregate: one tick per log actually scanned. */
interface AnalyticsProgress {
  done: number;
  total: number;
  current: string;
}

interface AnalyticsJobStatus {
  ok?: boolean;
  state?: "running" | "done" | "collected" | "error";
  done?: number;
  total?: number;
  current?: string;
  error?: string;
  payload?: AnalyticsPayload | null;
}

/** True when a stored % deviation exceeds the allowed absolute Δ (strictly outside tolerance). */
function absDeltaExceeds(v: unknown, maxAbsPct: number): boolean {
  if (typeof v !== "number" || !Number.isFinite(v)) return false;
  return Math.abs(v) > maxAbsPct;
}

export function AnalyticsView({ logDir, rows, status }: AnalyticsViewProps) {
  const { activeCluster, activePlatform } = useActiveCluster();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [data, setData] = useState<AnalyticsPayload | null>(null);
  const [progress, setProgress] = useState<AnalyticsProgress | null>(null);
  /** Highlight table rows when |Δ%| is greater than this value (NCCL + Nemotron % tables only). */
  const [perfVariationMaxAbsPct, setPerfVariationMaxAbsPct] = useState(5);
  const [tab, setTab] = useState(RACK_TAB);
  const [fleet, setFleet] = useState<BringupFleet | null>(null);
  const [fleetLoading, setFleetLoading] = useState(false);
  const [fleetError, setFleetError] = useState<string | null>(null);
  const [reportError] = useState("");
  const traysPerRack =
    activeCluster?.geometry?.trays_per_rack || activePlatform?.geometry?.trays_per_rack || 18;
  const gpusPerNode = activePlatform?.geometry?.gpus_per_node || 0;

  // The server's node inventory: real hostnames, placed in racks by the cluster's mapping mode and
  // naming templates, with live up/down and tray numbers. The browser does not re-derive nodes from
  // the rack rows -- on a translate cluster that invented <rack>-ctNN names with no status, and the
  // bring-up results (keyed by real hostname) then landed beside them as a second, rackless set.
  // The ct_down_unk expansion remains only for a server that sends no inventory.
  const inventoryNodes = useMemo(
    () => fleetNodesFromInventory(status?.nodes, status?.physical_rf0 ?? {}, status?.naming),
    [status],
  );

  const heatmapNodes = useMemo<NodeInfo[]>(() => {
    if (inventoryNodes) return inventoryNodes;
    const rf0 = status?.physical_rf0 ?? {};
    const naming = status?.naming;
    const out: NodeInfo[] = [];
    for (const r of rows) {
      out.push(...parseNodesFromRack(r.rack, r.ct_down_unk, rf0, traysPerRack, naming));
    }
    return out;
  }, [inventoryNodes, rows, status, traysPerRack]);

  const rackByNode = useMemo(() => {
    const map = new Map<string, string>();
    if (inventoryNodes) {
      for (const n of inventoryNodes) map.set(n.id, n.rack ?? UNPLACED_GROUP);
      return map;
    }
    const rf0 = status?.physical_rf0 ?? {};
    const naming = status?.naming;
    for (const r of rows) {
      for (const n of parseNodesFromRack(r.rack, r.ct_down_unk, rf0, traysPerRack, naming)) {
        map.set(n.id, r.rack);
      }
    }
    return map;
  }, [inventoryNodes, rows, status, traysPerRack]);

  const rackOf = useCallback(
    (nodeId: string) => {
      const known = rackByNode.get(nodeId);
      if (known) return known;
      // Bring-up knows the node and the inventory does not. With an inventory the placement is the
      // server's call, so the node is grouped as such rather than given a rack guessed from its name.
      if (inventoryNodes) return NOT_IN_INVENTORY_GROUP;
      const cut = nodeId.lastIndexOf("-");
      return cut > 0 ? nodeId.slice(0, cut) : nodeId;
    },
    [rackByNode, inventoryNodes],
  );

  const loadFleet = useCallback(async () => {
    setFleetLoading(true);
    setFleetError(null);
    try {
      setFleet(await getBringupFleet(heatmapNodes.map((n) => n.id)));
    } catch (e) {
      setFleetError(e instanceof Error ? e.message : String(e));
    } finally {
      setFleetLoading(false);
    }
  }, [heatmapNodes]);


  const bodyRows = useMemo(
    () => rows.map((r) => ({ rack: r.rack, nemo: r.nemo })),
    [rows]
  );

  /**
   * Start the aggregate as a job and poll it.
   *
   * A plain POST blocked for minutes on a large fleet with nothing to show for it. The server
   * knows how many logs it is about to read, so poll for done/total and drive a real bar.
   */
  const runAnalysis = useCallback(async () => {
    setLoading(true);
    setError("");
    setProgress(null);
    try {
      const start = await apiFetch("/api/analytics/aggregate-async", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        cache: "no-store",
        body: JSON.stringify({ log_dir: logDir || undefined, rows: bodyRows }),
      });
      if (start.status === 401) {
        // apiFetch already tried a renewal and, on failure, signed the UI out (the route is kept).
        return;
      }
      const started = (await start.json()) as { ok?: boolean; job_id?: string; error?: string };
      if (!start.ok || !started.ok || !started.job_id) {
        throw new Error(started.error || `HTTP ${start.status}`);
      }

      for (;;) {
        await new Promise((r) => setTimeout(r, ANALYTICS_POLL_MS));
        const res = await apiFetch(
          `/api/analytics/aggregate-status?job_id=${encodeURIComponent(started.job_id)}`,
          { credentials: "same-origin", cache: "no-store" },
        );
        if (res.status === 401) {
          // apiFetch already tried a renewal and, on failure, signed the UI out (the route is kept).
          return;
        }
        const st = (await res.json()) as AnalyticsJobStatus;
        if (!res.ok || !st.ok) throw new Error(st.error || `HTTP ${res.status}`);
        if (st.state === "running") {
          setProgress({ done: st.done ?? 0, total: st.total ?? 0, current: st.current ?? "" });
          continue;
        }
        if (st.state === "error") throw new Error(st.error || "analysis failed");
        const json = st.payload;
        if (!json || !json.ok) throw new Error(json?.error || "analysis returned no data");
        setData(json);
        return;
      }
    } catch (e) {
      setData(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
      setProgress(null);
    }
  }, [logDir, bodyRows]);

  const loadCachedAnalysis = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (logDir) params.set("log_dir", logDir);
      const res = await apiFetch(`/api/analytics/last?${params}`, {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (res.status === 401) {
        // apiFetch already tried a renewal and, on failure, signed the UI out (the route is kept).
        return;
      }
      if (res.status === 404) {
        return;
      }
      const json = (await res.json()) as AnalyticsPayload;
      if (!res.ok || !json.ok) {
        return;
      }
      setData((prev) => {
        if (prev != null && prev.cached === false) return prev;
        return json;
      });
    } catch {
      /* ignore cache load errors */
    }
  }, [logDir]);

  useEffect(() => {
    setData(null);
  }, [logDir]);

  useEffect(() => {
    void loadCachedAnalysis();
  }, [loadCachedAnalysis]);

  const sr = data?.single_rack;
  const mr = data?.multirack;

  // Both single-rack tables carry one row per rack (NCCL: per rack PER collective), so on a
  // 112-rack floor they run to hundreds of rows. Paged rather than capped: every row is a real
  // measurement someone may need to look up, so hiding the tail behind a "top N" would lose data.
  // Which collective the single-rack table and chart show; all_reduce unless the operator switches.
  const [collective, setCollective] = useState<`${CollectiveKey}_gbps`>("all_reduce_gbps");
  const collectiveKey = collective.replace(/_gbps$/, "") as CollectiveKey;
  const deviationRows = useMemo(
    () => (sr?.nccl_collectives_deviation_pct ?? EMPTY_ROWS).filter((r) => r.metric === collective),
    [sr, collective],
  );
  const ncclDeviationPage = useSortedPagedRows(deviationRows, deviationCell);
  // An older server sends only the all_reduce block; any other collective then has no series.
  const seriesBlock: BusBwSeriesBlock | undefined =
    sr?.nccl_series_by_collective?.[collectiveKey] ?? (collectiveKey === "all_reduce" ? sr?.nccl_ar_series : undefined);
  const nemotronPage = useSortedPagedRows(sr?.nemotron ?? EMPTY_ROWS, nemotronCell);

  const ncclLineData = useMemo(() => {
    if (!seriesBlock?.sizes_bytes?.length) return null;
    const { sizes_bytes, median_bus_bw_gbps, per_rack } = seriesBlock;
    const datasets = [
      {
        label: "Fleet median bus BW",
        data: sizes_bytes.map((sz, i) => ({ x: sz, y: median_bus_bw_gbps[i] ?? null })),
        spanGaps: true,
        borderColor: "#888",
        backgroundColor: "rgba(136,136,136,0.1)",
        tension: 0.15,
        pointRadius: 3,
      },
      ...per_rack.map((pr, idx) => ({
        label: pr.rack,
        data: sizes_bytes.map((sz, i) => {
          const y = pr.bus_bw_gbps[i];
          return { x: sz, y: y == null ? null : y };
        }),
        spanGaps: true,
        borderColor: CHART_COLORS[(idx + 1) % CHART_COLORS.length],
        backgroundColor: "transparent",
        tension: 0.15,
        pointRadius: 2,
        hidden: idx > 12,
      })),
    ];
    return {
      datasets,
    };
  }, [seriesBlock]);

  const nemotronBarTflopsData = useMemo(() => {
    if (!sr?.nemotron?.length) return null;
    return {
      labels: sr.nemotron.map((r) => r.rack),
      datasets: [
        {
          label: "% vs mean TFLOPs/GPU",
          data: sr.nemotron.map((r) => r.pct_vs_mean_tflops ?? 0),
          backgroundColor: CHART_COLORS[0],
        },
      ],
    };
  }, [sr]);

  const nemotronBarStepTimeData = useMemo(() => {
    if (!sr?.nemotron?.length) return null;
    return {
      labels: sr.nemotron.map((r) => r.rack),
      datasets: [
        {
          label: "% vs mean step time",
          data: sr.nemotron.map((r) => r.pct_vs_mean_step_time ?? 0),
          backgroundColor: CHART_COLORS[1],
        },
      ],
    };
  }, [sr]);

  const multirackNcclData = useMemo(() => {
    if (!mr?.nccl_series_by_log?.length) return null;
    const datasets = mr.nccl_series_by_log.map((log, idx) => ({
      label: log.label || log.basename,
      data: log.series.map((p) => ({ x: p.size_bytes, y: p.bus_bw_gbps })),
      borderColor: CHART_COLORS[idx % CHART_COLORS.length],
      backgroundColor: "transparent",
      tension: 0.15,
      pointRadius: 2,
    }));
    return { datasets };
  }, [mr]);

  const scalingLineData = useMemo(() => {
    if (!mr?.nemotron_scaling?.length) return null;
    const sorted = [...mr.nemotron_scaling].sort((a, b) => a.n_racks - b.n_racks);
    return {
      datasets: [
        {
          label: "Mean TFLOPs/GPU",
          data: sorted.map((s) => ({ x: s.n_racks, y: s.mean_tflops_per_gpu })),
          borderColor: CHART_COLORS[0],
          yAxisID: "y",
          tension: 0.2,
        },
        {
          label: "Mean step time (s)",
          data: sorted.map((s) => ({ x: s.n_racks, y: s.mean_step_s })),
          borderColor: CHART_COLORS[1],
          yAxisID: "y1",
          tension: 0.2,
        },
      ],
    };
  }, [mr]);

  /** Per-bucket scaling efficiency vs the smallest n_racks row (weak-scaling style: hold per-GPU throughput and step time). */
  const nemotronScalingTableRows = useMemo(() => {
    if (!mr?.nemotron_scaling?.length) {
      return { baselineNRacks: null as number | null, rows: [] as { s: NemotronScalingRow; tflopsEffPct: number | null; stepEffPct: number | null }[] };
    }
    const sorted = [...mr.nemotron_scaling].sort((a, b) => a.n_racks - b.n_racks);
    // Baseline is the smallest bucket that actually measured something. A bucket present only to
    // report runs with no metrics carries null means, and using it would null every efficiency.
    const base = sorted.find((s) => Number(s.mean_tflops_per_gpu) > 0 && Number(s.mean_step_s) > 0) ?? sorted[0];
    const baseTf = Number(base.mean_tflops_per_gpu);
    const baseStep = Number(base.mean_step_s);
    const rows = sorted.map((s) => {
      const tf = Number(s.mean_tflops_per_gpu);
      const st = Number(s.mean_step_s);
      const tflopsEffPct =
        baseTf > 0 && tf > 0 && Number.isFinite(baseTf) && Number.isFinite(tf) ? (tf / baseTf) * 100 : null;
      const stepEffPct =
        baseStep > 0 && st > 0 && Number.isFinite(baseStep) && Number.isFinite(st) ? (baseStep / st) * 100 : null;
      return { s, tflopsEffPct, stepEffPct };
    });
    return { baselineNRacks: base.n_racks, rows };
  }, [mr]);

  const formatNcclSizeTick = (tickValue: number | string) => {
    const v = typeof tickValue === "number" ? tickValue : Number(tickValue);
    if (!Number.isFinite(v) || v <= 0) return "";
    if (v >= 1e12) return `${(v / 1e12).toFixed(1)}T`;
    if (v >= 1e9) return `${(v / 1e9).toFixed(1)}G`;
    if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
    if (v >= 1e3) return `${(v / 1e3).toFixed(0)}k`;
    return String(Math.round(v));
  };

  const chartOptionsLogX = {
    responsive: true,
    maintainAspectRatio: false,
    parsing: false as const,
    scales: {
      x: {
        type: "logarithmic" as const,
        suggestedMin: 1,
        title: { display: true, text: "Message size (bytes, log scale)" },
        ticks: {
          callback: (v: number | string) => formatNcclSizeTick(v),
        },
      },
      y: {
        title: { display: true, text: "Out-of-place bus BW (GB/s)" },
        beginAtZero: true,
      },
    },
    plugins: {
      legend: { position: "bottom" as const, labels: { boxWidth: 12 } },
      tooltip: {
        callbacks: {
          title(tooltipItems: { raw?: unknown }[]) {
            const raw = tooltipItems[0]?.raw as { x?: number } | undefined;
            const x = raw?.x;
            return x != null ? `size: ${x.toLocaleString()} B` : "";
          },
        },
      },
    },
  };

  const scalingOptions = {
    responsive: true,
    maintainAspectRatio: false,
    parsing: false as const,
    scales: {
      x: {
        type: "linear" as const,
        title: { display: true, text: "Rack count (inferred from log name / host list)" },
        ticks: { stepSize: 1 },
      },
      y: {
        type: "linear" as const,
        position: "left" as const,
        title: { display: true, text: "Mean TFLOPs/GPU" },
      },
      y1: {
        type: "linear" as const,
        position: "right" as const,
        title: { display: true, text: "Mean step time (s)" },
        grid: { drawOnChartArea: false },
      },
    },
    plugins: { legend: { position: "bottom" as const } },
  };

  const perfToleranceTooltip =
    "Rows highlight red when outside tolerance: |% vs median| (NCCL deviation) or |% vs fleet mean| (Nemotron) exceeds this value. Band is |Δ%| ≤ value (strictly greater fails). Nemotron scaling efficiency table is not checked.";

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}>
      <PageHeader
        contentId="analytics"
        title="Analytics"
        description="Compare NCCL and Nemotron metrics from parser output: single-rack uses the newest successful 1Rack log per rack with validation done; multirack uses successful multirack logs only."
        actions={
          <MuiTooltip title={perfToleranceTooltip} placement="left" enterDelay={400}>
            <Paper
              variant="outlined"
              component="div"
              sx={{
                p: 1,
                minWidth: 168,
                maxWidth: 200,
                flexShrink: 0,
                cursor: "default",
              }}
            >
              <Text kind="body/regular/sm" style={{ display: "block", fontWeight: 600, lineHeight: 1.2, marginBottom: 2 }}>
                Perf Δ% tolerance
              </Text>
              <Slider
                size="small"
                value={perfVariationMaxAbsPct}
                onChange={(_, v) => setPerfVariationMaxAbsPct(Array.isArray(v) ? v[0] : v)}
                min={0}
                max={10}
                step={0.1}
                valueLabelDisplay="auto"
                valueLabelFormat={(x) => `±${Number(x).toFixed(1)}%`}
                aria-label="Maximum absolute performance deviation before row highlight"
                sx={{ mt: 0.25, mb: 0.25 }}
              />
              <Text kind="body/regular/sm" style={{ display: "block", lineHeight: 1.2, color: "var(--text-color-secondary)" }}>
                ≤{perfVariationMaxAbsPct.toFixed(1)}% · hover for help
              </Text>
            </Paper>
          </MuiTooltip>
        }
      />

      <Tabs
        items={[
          { children: "Rack Level", value: RACK_TAB },
          { children: "Node Level", value: NODE_TAB },
        ]}
        value={tab}
        // Fetch on activation rather than on mount: the fleet call reaches the cluster, so the
        // rack-level view should not pay for it. Fetched once; the panel has its own refresh.
        onValueChange={(next) => {
          setTab(next);
          if (next === NODE_TAB && !fleet && !fleetLoading) void loadFleet();
        }}
        // The report covers the whole cluster, so it belongs to the panel rather than to a
        // tab: it stays reachable from either view.
        slotEnd={
          <Box sx={{ ml: "auto", display: "flex", alignItems: "center", gap: 1 }}>
            {reportError && (
              <Text kind="body/regular/sm" style={{ color: "#d32f2f" }}>
                {reportError}
              </Text>
            )}
          </Box>
        }
      />

      {tab === NODE_TAB && (
        <Box sx={{ mt: 2 }}>
          <NodeHeatmap
            fleet={fleet}
            nodes={heatmapNodes}
            rackOf={rackOf}
            onlyListedNodes={inventoryNodes !== null}
            loading={fleetLoading}
            error={fleetError}
            onRefresh={() => void loadFleet()}
            clusterName={activeCluster?.name}
          />
        </Box>
      )}

      {tab === RACK_TAB && (
      <>
      <IssueHealthOverview />
      {/* Reports describe the cluster and its racks, so the control belongs with that content
          rather than in the page header above both tabs. */}
      <ReportGenerator />
      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Text kind="body/regular/md" style={{ display: "block", marginBottom: 12, color: "var(--text-color-secondary)" }}>
          Reads every relevant log once with{" "}
          <Box component="span" sx={{ fontFamily: "monospace", fontSize: "0.9em" }}>
            nvis_logscan
          </Box>{" "}
          and reports progress per file. Uses the current log directory
          {logDir ? ` (${logDir})` : " (server default)"} and the latest rack status rows you have loaded.
          The server keeps the last successful aggregate under{" "}
          <Box component="span" sx={{ fontFamily: "monospace", fontSize: "0.85em" }}>
            var/cache/analytics_aggregate_cache.json
          </Box>{" "}
          per log directory; this page loads it automatically when you open Analytics.
        </Text>
        <Button kind="primary" color="brand" onClick={() => void runAnalysis()} disabled={loading || !bodyRows.length}>
          {loading ? "Running…" : "Run analysis"}
        </Button>
        {!bodyRows.length && (
          <Text kind="body/regular/md" style={{ marginTop: 8, display: "block", color: "#ed6c02" }}>
            No rack rows loaded yet — open Single Rack so status loads, then return here.
          </Text>
        )}
        {/* Determinate once the server has enumerated the corpus; a spinner only for the gap
            before the first tick, when the total is genuinely not known yet. */}
        {loading && (
          <Box sx={{ mt: 2 }}>
            {progress && progress.total > 0 ? (
              <>
                <Box sx={{ display: "flex", justifyContent: "space-between", mb: 0.5 }}>
                  <Text kind="body/regular/md">
                    Scanning logs — {progress.done} of {progress.total}
                  </Text>
                  <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                    {Math.round((progress.done / progress.total) * 100)}%
                  </Text>
                </Box>
                <ProgressBar
                  value={Math.round((progress.done / progress.total) * 100)}
                  aria-label="Analytics log scan progress"
                />
                {progress.current && (
                  <Text
                    kind="mono/sm"
                    style={{
                      display: "block",
                      marginTop: 6,
                      color: "var(--text-color-secondary)",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {progress.current}
                  </Text>
                )}
              </>
            ) : (
              <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
                <CircularProgress size={22} />
                <Text kind="body/regular/md">Finding logs…</Text>
              </Box>
            )}
          </Box>
        )}
      </Paper>

      {error && (
        <Banner status="error" kind="inline" style={{ marginBottom: 16 }}>
          {error}
        </Banner>
      )}

      {data?.cached && data.analyzed_at && (
        <div style={{ marginBottom: 16 }}>
          <Banner status="info">
            Showing last saved analysis ({new Date(data.analyzed_at).toLocaleString()}). Run analysis again to refresh
            after new validation logs or rack status changes.
          </Banner>
        </div>
      )}

      {data && !data.cached && data.analyzed_at && (
        <Text kind="body/regular/sm" style={{ display: "block", marginBottom: 8, color: "var(--text-color-secondary)" }}>
          Last run: {new Date(data.analyzed_at).toLocaleString()}
        </Text>
      )}

      {sr && (
        <>
          <Text kind="title/sm" style={{ display: "block", marginTop: 16, marginBottom: 8, fontWeight: 600 }}>
            1 — Single rack
          </Text>

          <Box sx={{ display: "flex", alignItems: "flex-end", gap: 2, mb: 1, flexWrap: "wrap" }}>
            <Text kind="body/bold/sm" style={{ flex: 1, color: "var(--text-color-secondary)" }}>
              NCCL — peak {collectiveKey} deviation vs fleet median (same log as newest 1Rack file per done rack)
            </Text>
            <Box sx={{ minWidth: 220 }}>
              <FormField name="analytics-collective" slotLabel="Collective">
                <Select
                  items={COLLECTIVE_OPTIONS}
                  value={collective}
                  onValueChange={(v) => setCollective((v || "all_reduce_gbps") as `${CollectiveKey}_gbps`)}
                />
              </FormField>
            </Box>
          </Box>
          <TableContainer component={Paper} variant="outlined" sx={{ mb: 2 }}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  {DEVIATION_COLUMNS.map(([label, align], col) => (
                    <SortableHeaderCell
                      key={label}
                      label={label}
                      col={col}
                      align={align}
                      variant="plain"
                      sortCol={ncclDeviationPage.sortCol}
                      sortDir={ncclDeviationPage.sortDir}
                      onToggle={ncclDeviationPage.toggleSort}
                    />
                  ))}
                </TableRow>
              </TableHead>
              <TableBody>
                {ncclDeviationPage.visible.map((r, i) => {
                  const rowFail = absDeltaExceeds(r.pct_vs_median, perfVariationMaxAbsPct);
                  return (
                  <TableRow
                    key={`${r.metric}-${r.rack}-${i}`}
                    sx={(theme) =>
                      rowFail
                        ? { bgcolor: alpha(theme.palette.error.main, 0.14), "& > *": { borderColor: alpha(theme.palette.error.main, 0.25) } }
                        : {}
                    }
                  >
                    <TableCell>{r.metric}</TableCell>
                    <TableCell>{r.rack}</TableCell>
                    <TableCell sx={{ maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis" }} title={r.log_file}>
                      {r.log_file}
                    </TableCell>
                    <TableCell align="right">{r.value_gbps}</TableCell>
                    <TableCell align="right">{r.fleet_median_gbps}</TableCell>
                    <TableCell align="right">{r.pct_vs_median}</TableCell>
                  </TableRow>
                  );
                })}
                {!deviationRows.length && (
                  <TableRow>
                    <TableCell colSpan={6}>
                      <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
                        No {collectiveKey} rows (no done rack's newest NCCL log measured it).
                      </Text>
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </TableContainer>
          <TablePager {...ncclDeviationPage} noun="rows" />

          <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 4, color: "var(--text-color-secondary)" }}>
            NCCL — {collectiveKey} bus BW vs message size (fleet median + each rack)
          </Text>
          <Paper variant="outlined" sx={{ p: 2, mb: 2, height: 380 }}>
            {ncclLineData ? <Line data={ncclLineData} options={chartOptionsLogX} /> : (
              <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>No {collectiveKey} series data.</Text>
            )}
          </Paper>

          <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 4, color: "var(--text-color-secondary)" }}>
            Nemotron — step time &amp; TFLOPs/GPU vs fleet mean; step_time CV% within the training window
          </Text>
          <TableContainer component={Paper} variant="outlined" sx={{ mb: 2 }}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  {NEMOTRON_COLUMNS.map(([label, align], col) => (
                    <SortableHeaderCell
                      key={label}
                      label={label}
                      col={col}
                      align={align}
                      variant="plain"
                      sortCol={nemotronPage.sortCol}
                      sortDir={nemotronPage.sortDir}
                      onToggle={nemotronPage.toggleSort}
                    />
                  ))}
                </TableRow>
              </TableHead>
              <TableBody>
                {nemotronPage.visible.map((r) => {
                  const rowFail =
                    absDeltaExceeds(r.pct_vs_mean_tflops, perfVariationMaxAbsPct) ||
                    absDeltaExceeds(r.pct_vs_mean_step_time, perfVariationMaxAbsPct);
                  return (
                  <TableRow
                    key={r.rack}
                    sx={(theme) =>
                      rowFail
                        ? { bgcolor: alpha(theme.palette.error.main, 0.14), "& > *": { borderColor: alpha(theme.palette.error.main, 0.25) } }
                        : {}
                    }
                  >
                    <TableCell>{r.rack}</TableCell>
                    <TableCell sx={{ maxWidth: 220, overflow: "hidden", textOverflow: "ellipsis" }} title={r.log_file}>
                      {r.log_file}
                    </TableCell>
                    <TableCell align="right">{r.median_step_time ?? "—"}</TableCell>
                    <TableCell align="right">{r.tflops_per_gpu ?? "—"}</TableCell>
                    <TableCell align="right">{r.step_time_cv_pct ?? "—"}</TableCell>
                    <TableCell align="right">{r.pct_vs_mean_tflops ?? "—"}</TableCell>
                    <TableCell align="right">{r.pct_vs_mean_step_time ?? "—"}</TableCell>
                  </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </TableContainer>
          <TablePager {...nemotronPage} noun="racks" />

          <Box
            sx={{
              display: "grid",
              gridTemplateColumns: { xs: "1fr", lg: "1fr 1fr" },
              gap: 2,
              mb: 3,
            }}
          >
            <Paper variant="outlined" sx={{ p: 2, height: 320 }}>
              {nemotronBarTflopsData ? (
                <Bar data={nemotronBarTflopsData} options={nemotronPctBarOptionsTflops} />
              ) : (
                <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
                  No Nemotron rows.
                </Text>
              )}
            </Paper>
            <Paper variant="outlined" sx={{ p: 2, height: 320 }}>
              {nemotronBarStepTimeData ? (
                <Bar data={nemotronBarStepTimeData} options={nemotronPctBarOptionsStepTime} />
              ) : (
                <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
                  No Nemotron rows.
                </Text>
              )}
            </Paper>
          </Box>
        </>
      )}

      {mr && (
        <>
          <Text kind="title/sm" style={{ display: "block", marginTop: 16, marginBottom: 8, fontWeight: 600 }}>
            2 — Multirack
          </Text>
          <Text kind="body/regular/md" style={{ display: "block", marginBottom: 8, color: "var(--text-color-secondary)" }}>
            NCCL: one line series per completed multirack log (label = rack combination). Nemotron scaling tables: Rack
            1 = all successful single-rack (1Rack) logs for done racks; Rack 2+ = multirack logs grouped by inferred rack
            count (e.g.{" "}
            <Box component="span" sx={{ fontFamily: "monospace" }}>2rack</Box> in the filename or distinct{" "}
            <Box component="span" sx={{ fontFamily: "monospace" }}>pN-rMM</Box> tokens).
          </Text>

          <Paper variant="outlined" sx={{ p: 2, mb: 2, height: 400 }}>
            {multirackNcclData ? <Line data={multirackNcclData} options={chartOptionsLogX} /> : (
              <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>No multirack NCCL series.</Text>
            )}
          </Paper>

          <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 4, color: "var(--text-color-secondary)" }}>
            NCCL — peak bus bandwidth by scale (GB/s). Runs are grouped by the node count they actually used, from the
            log itself; racks are that count ÷ {traysPerRack} nodes per rack. Each value is the mean of the logs&apos;
            per-collective peaks, and a dash means no log in the row measured that collective.
          </Text>
          <TableContainer component={Paper} variant="outlined" sx={{ mb: 2 }}>
            <Table size="small" aria-label="NCCL scaling">
              <TableHead>
                <TableRow>
                  <TableCell align="right">Nodes</TableCell>
                  <TableCell align="right">GPUs</TableCell>
                  <TableCell align="right">≈ Racks</TableCell>
                  <TableCell align="right"># logs</TableCell>
                  <TableCell align="right">All-reduce</TableCell>
                  <TableCell align="right">All-gather</TableCell>
                  <TableCell align="right">All-to-all</TableCell>
                  <TableCell align="right">Reduce-scatter</TableCell>
                  <TableCell align="right">Send/recv</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {(mr.nccl_scaling ?? []).length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={9}>
                      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                        No NCCL scaling data yet.
                      </Text>
                    </TableCell>
                  </TableRow>
                ) : (
                  (mr.nccl_scaling ?? []).map((r) => (
                    <TableRow key={r.num_nodes ?? "unknown"}>
                      <TableCell align="right">{r.num_nodes ?? "unknown"}</TableCell>
                      <GpuCountCell measured={r.num_gpus} nodes={r.num_nodes} gpusPerNode={gpusPerNode} />
                      <TableCell align="right">
                        {r.num_nodes ? Math.round((r.num_nodes / traysPerRack) * 10) / 10 : "—"}
                      </TableCell>
                      <LogCountCell nLogs={r.n_logs} nNoMetrics={r.n_logs_no_metrics} />
                      <TableCell align="right">{metricOrDash(r.mean_all_reduce_gbps)}</TableCell>
                      <TableCell align="right">{metricOrDash(r.mean_all_gather_gbps)}</TableCell>
                      <TableCell align="right">{metricOrDash(r.mean_alltoall_gbps)}</TableCell>
                      <TableCell align="right">{metricOrDash(r.mean_reduce_scatter_gbps)}</TableCell>
                      <TableCell align="right">{metricOrDash(r.mean_sendrecv_gbps)}</TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </TableContainer>

          <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 4, color: "var(--text-color-secondary)" }}>
            Nemotron — step time and TFLOPs by rack count. Only NeMo training logs appear here; an NCCL log never does.
          </Text>
          <Box sx={{ mb: 2 }}>
            <TableContainer component={Paper} variant="outlined">
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Rack</TableCell>
                    <TableCell align="right"># logs</TableCell>
                    <TableCell align="right">Mean TFLOPs/GPU</TableCell>
                    <TableCell align="right">Mean step (s)</TableCell>
                    <TableCell align="right">TFLOPs eff. (%)</TableCell>
                    <TableCell align="right">Step eff. (%)</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {nemotronScalingTableRows.rows.map(({ s, tflopsEffPct, stepEffPct }) => (
                    <TableRow key={s.n_racks}>
                      <TableCell>{s.n_racks}</TableCell>
                      <LogCountCell nLogs={s.n_logs} nNoMetrics={s.n_logs_no_metrics} />
                      <TableCell align="right">{metricOrDash(s.mean_tflops_per_gpu)}</TableCell>
                      <TableCell align="right">{metricOrDash(s.mean_step_s)}</TableCell>
                      <TableCell align="right">
                        {tflopsEffPct == null ? "—" : `${tflopsEffPct.toFixed(1)}%`}
                      </TableCell>
                      <TableCell align="right">
                        {stepEffPct == null ? "—" : `${stepEffPct.toFixed(1)}%`}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
            {nemotronScalingTableRows.baselineNRacks != null && nemotronScalingTableRows.rows.length > 0 ? (
              <Text kind="body/regular/sm" style={{ display: "block", marginTop: 8, paddingLeft: 4, paddingRight: 4, color: "var(--text-color-secondary)" }}>
                Scaling efficiency vs the smallest Rack bucket (includes 1R single-rack logs and multirack logs): TFLOPs
                eff. = 100 × (mean TFLOPs/GPU ÷ baseline); step eff. = 100 × (baseline mean step ÷ mean step). 100%
                matches baseline; higher step eff. means relatively faster steps. A “no metrics” tag counts successful
                runs whose step timings could not be averaged (too few samples survived the warmup); they are reported
                here rather than dropped, but contribute to no mean.
              </Text>
            ) : null}
          </Box>

          <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 4, color: "var(--text-color-secondary)" }}>
            Nemotron — throughput &amp; time to train 1T tokens (Rack = inferred rack count: 1R done racks + multirack
            logs; # logs = count in bucket; nodes/# GPU from one newest log in the bucket; other numeric columns are
            means). Throughput = global_batch × seq_len ÷ mean step when{" "}
            <Box component="span" sx={{ fontFamily: "monospace" }}>-gb</Box> is found in each log.
          </Text>
          <TableContainer component={Paper} variant="outlined" sx={{ mb: 1 }}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Rack</TableCell>
                  <TableCell align="right"># logs</TableCell>
                  <TableCell align="right">Nodes</TableCell>
                  <TableCell align="right"># GPU</TableCell>
                  <TableCell align="right">Step (s) mean</TableCell>
                  <TableCell align="right">perGPU TFLOPs mean</TableCell>
                  <TableCell align="right">Throughput tok/s mean</TableCell>
                  <TableCell align="right">Throughput tok/s/GPU mean</TableCell>
                  <TableCell align="right">Time to train 1T tok (days)</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {(mr.nemotron_throughput_scaling ?? []).length ? (
                  (mr.nemotron_throughput_scaling ?? []).map((row) => (
                    <TableRow key={row.n_racks}>
                      <TableCell>{row.n_racks}</TableCell>
                      <LogCountCell nLogs={row.n_logs} nNoMetrics={row.n_logs_no_metrics} />
                      <TableCell align="right">{row.num_nodes == null ? "—" : row.num_nodes}</TableCell>
                      <TableCell align="right">{row.num_gpus == null ? "—" : row.num_gpus}</TableCell>
                      <TableCell align="right">{metricOrDash(row.mean_step_s)}</TableCell>
                      <TableCell align="right">{metricOrDash(row.mean_tflops_per_gpu)}</TableCell>
                      <TableCell align="right">
                        {row.mean_throughput_tokens_s == null
                          ? "—"
                          : row.mean_throughput_tokens_s.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                      </TableCell>
                      <TableCell align="right">
                        {row.mean_throughput_tokens_s_per_gpu == null
                          ? "—"
                          : row.mean_throughput_tokens_s_per_gpu.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                      </TableCell>
                      <TableCell align="right">
                        {row.time_to_train_1t_days == null
                          ? "—"
                          : row.time_to_train_1t_days.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                      </TableCell>
                    </TableRow>
                  ))
                ) : (
                  <TableRow>
                    <TableCell colSpan={9}>
                      <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
                        No Nemotron throughput rows (needs multirack logs with Nemotron stats).
                      </Text>
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </TableContainer>
          <Text kind="body/regular/sm" style={{ display: "block", marginBottom: 16, paddingLeft: 4, paddingRight: 4, color: "var(--text-color-secondary)" }}>
            Same Rack buckets as the table above. Nodes and # GPU come from the newest log in each bucket (by file
            mtime). Time to train 1T tokens (days) = 10^12 ÷ (mean throughput tokens/s × 86,400). Throughput uses global
            batch from <Box component="span" sx={{ fontFamily: "monospace" }}>-gb</Box> × sequence length (parsed when
            possible, else 8192) ÷ per-log step time, then averaged within the bucket.
          </Text>

          <Paper variant="outlined" sx={{ p: 2, mb: 2, height: 340 }}>
            {scalingLineData ? <Line data={scalingLineData} options={scalingOptions} /> : (
              <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>No multirack Nemotron scaling buckets.</Text>
            )}
          </Paper>
        </>
      )}
      </>
      )}
    </Box>
  );
}
