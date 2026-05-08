import { useState, useCallback, useMemo } from "react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Paper from "@mui/material/Paper";
import Typography from "@mui/material/Typography";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Alert from "@mui/material/Alert";
import CircularProgress from "@mui/material/CircularProgress";
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
import { PageHeader } from "../components/layout/PageHeader";
import type { RackRow } from "../types/api";

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

type AnalyticsPayload = {
  ok: boolean;
  error?: string;
  single_rack?: {
    nccl_collectives: { rack: string; log_file: string; all_reduce_gbps: number | null; all_gather_gbps: number | null; alltoall_gbps: number | null }[];
    nccl_collectives_deviation_pct: {
      metric: string;
      rack: string;
      log_file: string;
      value_gbps: number;
      fleet_median_gbps: number;
      pct_vs_median: number;
    }[];
    nccl_ar_series: {
      sizes_bytes: number[];
      median_bus_bw_gbps: (number | null)[];
      per_rack: { rack: string; log_file: string; bus_bw_gbps: (number | null)[]; pct_vs_median: (number | null)[] }[];
    };
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
    nemotron_scaling: { n_racks: number; n_logs: number; mean_tflops_per_gpu: number; mean_step_s: number }[];
    nemotron_throughput_scaling?: {
      n_racks: number;
      n_logs: number;
      num_nodes: number | null;
      num_gpus: number | null;
      mean_step_s: number;
      mean_tflops_per_gpu: number;
      mean_throughput_tokens_s: number | null;
      mean_throughput_tokens_s_per_gpu: number | null;
      time_to_train_1t_days: number | null;
    }[];
  };
};

type NemotronScalingRow = NonNullable<AnalyticsPayload["multirack"]>["nemotron_scaling"][number];

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
}

/** True when a stored % deviation exceeds the allowed absolute Δ (strictly outside tolerance). */
function absDeltaExceeds(v: unknown, maxAbsPct: number): boolean {
  if (typeof v !== "number" || !Number.isFinite(v)) return false;
  return Math.abs(v) > maxAbsPct;
}

export function AnalyticsView({ logDir, rows }: AnalyticsViewProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [data, setData] = useState<AnalyticsPayload | null>(null);
  /** Highlight table rows when |Δ%| is greater than this value (NCCL + Nemotron % tables only). */
  const [perfVariationMaxAbsPct, setPerfVariationMaxAbsPct] = useState(5);

  const bodyRows = useMemo(
    () => rows.map((r) => ({ rack: r.rack, nemo: r.nemo })),
    [rows]
  );

  const runAnalysis = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await fetch("/api/analytics/aggregate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ log_dir: logDir || undefined, rows: bodyRows }),
      });
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
        return;
      }
      const json = (await res.json()) as AnalyticsPayload;
      if (!res.ok || !json.ok) {
        throw new Error(json.error || `HTTP ${res.status}`);
      }
      setData(json);
    } catch (e) {
      setData(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [logDir, bodyRows]);

  const sr = data?.single_rack;
  const mr = data?.multirack;

  const ncclLineData = useMemo(() => {
    if (!sr?.nccl_ar_series?.sizes_bytes?.length) return null;
    const { sizes_bytes, median_bus_bw_gbps, per_rack } = sr.nccl_ar_series;
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
  }, [sr]);

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
    const base = sorted[0];
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
    <Box sx={{ maxWidth: 1400, mx: "auto", p: 4 }}>
      <PageHeader
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
              <Typography variant="caption" sx={{ display: "block", fontWeight: 600, lineHeight: 1.2, mb: 0.25 }}>
                Perf Δ% tolerance
              </Typography>
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
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", lineHeight: 1.2 }}>
                ≤{perfVariationMaxAbsPct.toFixed(1)}% · hover for help
              </Typography>
            </Paper>
          </MuiTooltip>
        }
      />

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          Runs{" "}
          <Box component="span" sx={{ fontFamily: "monospace", fontSize: "0.9em" }}>
            parser.sh --json
          </Box>{" "}
          on many logs — can take several minutes on large fleets. Uses the current log directory
          {logDir ? ` (${logDir})` : " (server default)"} and the latest rack status rows you have loaded.
        </Typography>
        <Button variant="contained" onClick={() => void runAnalysis()} disabled={loading || !bodyRows.length}>
          {loading ? "Running…" : "Run analysis"}
        </Button>
        {!bodyRows.length && (
          <Typography variant="body2" color="warning.main" sx={{ mt: 1, display: "block" }}>
            No rack rows loaded yet — open Single Rack so status loads, then return here.
          </Typography>
        )}
        {loading && (
          <Box sx={{ display: "flex", alignItems: "center", gap: 1, mt: 2 }}>
            <CircularProgress size={22} />
            <Typography variant="body2">Parsing logs…</Typography>
          </Box>
        )}
      </Paper>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}

      {sr && (
        <>
          <Typography variant="h6" sx={{ mt: 2, mb: 1, fontWeight: 600 }}>
            1 — Single rack
          </Typography>

          <Typography variant="subtitle2" color="text.secondary" sx={{ mb: 0.5 }}>
            NCCL — peak collectives deviation vs fleet median (same log as newest 1Rack file per done rack)
          </Typography>
          <TableContainer component={Paper} variant="outlined" sx={{ mb: 2 }}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Metric</TableCell>
                  <TableCell>Rack</TableCell>
                  <TableCell>Log</TableCell>
                  <TableCell align="right">Value (GB/s)</TableCell>
                  <TableCell align="right">Fleet median</TableCell>
                  <TableCell align="right">% vs median</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {(sr.nccl_collectives_deviation_pct ?? []).map((r, i) => {
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
                {!(sr.nccl_collectives_deviation_pct ?? []).length && (
                  <TableRow>
                    <TableCell colSpan={6}>
                      <Typography variant="body2" color="text.secondary">
                        No NCCL scalar rows (missing parser fields or no done racks with logs).
                      </Typography>
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </TableContainer>

          <Typography variant="subtitle2" color="text.secondary" sx={{ mb: 0.5 }}>
            NCCL — all_reduce bus BW vs message size (fleet median + each rack)
          </Typography>
          <Paper variant="outlined" sx={{ p: 2, mb: 2, height: 380 }}>
            {ncclLineData ? <Line data={ncclLineData} options={chartOptionsLogX} /> : (
              <Typography variant="body2" color="text.secondary">No all_reduce series data.</Typography>
            )}
          </Paper>

          <Typography variant="subtitle2" color="text.secondary" sx={{ mb: 0.5 }}>
            Nemotron — step time &amp; TFLOPs/GPU vs fleet mean; step_time CV% within the training window
          </Typography>
          <TableContainer component={Paper} variant="outlined" sx={{ mb: 2 }}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Rack</TableCell>
                  <TableCell>Log</TableCell>
                  <TableCell align="right">Median step (s)</TableCell>
                  <TableCell align="right">TFLOPs/GPU</TableCell>
                  <TableCell align="right">Step CV%</TableCell>
                  <TableCell align="right">% vs mean TFLOPs</TableCell>
                  <TableCell align="right">% vs mean step</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {(sr.nemotron ?? []).map((r) => {
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
                <Typography variant="body2" color="text.secondary">
                  No Nemotron rows.
                </Typography>
              )}
            </Paper>
            <Paper variant="outlined" sx={{ p: 2, height: 320 }}>
              {nemotronBarStepTimeData ? (
                <Bar data={nemotronBarStepTimeData} options={nemotronPctBarOptionsStepTime} />
              ) : (
                <Typography variant="body2" color="text.secondary">
                  No Nemotron rows.
                </Typography>
              )}
            </Paper>
          </Box>
        </>
      )}

      {mr && (
        <>
          <Typography variant="h6" sx={{ mt: 2, mb: 1, fontWeight: 600 }}>
            2 — Multirack
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
            NCCL: one line series per completed multirack log (label = rack combination). Nemotron scaling tables: Rack
            1 = all successful single-rack (1Rack) logs for done racks; Rack 2+ = multirack logs grouped by inferred rack
            count (e.g.{" "}
            <Box component="span" sx={{ fontFamily: "monospace" }}>2rack</Box> in the filename or distinct{" "}
            <Box component="span" sx={{ fontFamily: "monospace" }}>pN-rMM</Box> tokens).
          </Typography>

          <Paper variant="outlined" sx={{ p: 2, mb: 2, height: 400 }}>
            {multirackNcclData ? <Line data={multirackNcclData} options={chartOptionsLogX} /> : (
              <Typography variant="body2" color="text.secondary">No multirack NCCL series.</Typography>
            )}
          </Paper>

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
                      <TableCell align="right">{s.n_logs}</TableCell>
                      <TableCell align="right">{s.mean_tflops_per_gpu}</TableCell>
                      <TableCell align="right">{s.mean_step_s}</TableCell>
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
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1, px: 0.5 }}>
                Scaling efficiency vs the smallest Rack bucket (includes 1R single-rack logs and multirack logs): TFLOPs
                eff. = 100 × (mean TFLOPs/GPU ÷ baseline); step eff. = 100 × (baseline mean step ÷ mean step). 100%
                matches baseline; higher step eff. means relatively faster steps.
              </Typography>
            ) : null}
          </Box>

          <Typography variant="subtitle2" color="text.secondary" sx={{ mb: 0.5 }}>
            Nemotron — throughput &amp; time to train 1T tokens (Rack = inferred rack count: 1R done racks + multirack
            logs; # logs = count in bucket; nodes/# GPU from one newest log in the bucket; other numeric columns are
            means). Throughput = global_batch × seq_len ÷ mean step when{" "}
            <Box component="span" sx={{ fontFamily: "monospace" }}>-gb</Box> is found in each log.
          </Typography>
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
                      <TableCell align="right">{row.n_logs}</TableCell>
                      <TableCell align="right">{row.num_nodes == null ? "—" : row.num_nodes}</TableCell>
                      <TableCell align="right">{row.num_gpus == null ? "—" : row.num_gpus}</TableCell>
                      <TableCell align="right">{row.mean_step_s}</TableCell>
                      <TableCell align="right">{row.mean_tflops_per_gpu}</TableCell>
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
                      <Typography variant="body2" color="text.secondary">
                        No Nemotron throughput rows (needs multirack logs with Nemotron stats).
                      </Typography>
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          </TableContainer>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 2, px: 0.5 }}>
            Same Rack buckets as the table above. Nodes and # GPU come from the newest log in each bucket (by file
            mtime). Time to train 1T tokens (days) = 10^12 ÷ (mean throughput tokens/s × 86,400). Throughput uses global
            batch from <Box component="span" sx={{ fontFamily: "monospace" }}>-gb</Box> × sequence length (parsed when
            possible, else 8192) ÷ per-log step time, then averaged within the bucket.
          </Typography>

          <Paper variant="outlined" sx={{ p: 2, mb: 2, height: 340 }}>
            {scalingLineData ? <Line data={scalingLineData} options={scalingOptions} /> : (
              <Typography variant="body2" color="text.secondary">No multirack Nemotron scaling buckets.</Typography>
            )}
          </Paper>
        </>
      )}
    </Box>
  );
}
