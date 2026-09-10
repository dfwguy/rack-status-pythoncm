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
import { useEffect, useMemo, useState, type ReactNode } from "react";
import Box from "@mui/material/Box";
import FormControl from "@mui/material/FormControl";
import InputLabel from "@mui/material/InputLabel";
import MenuItem from "@mui/material/MenuItem";
import Select from "@mui/material/Select";
import CircularProgress from "@mui/material/CircularProgress";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  BarElement,
  LineElement,
  PointElement,
  Title,
  Tooltip,
  Legend,
  Filler,
} from "chart.js";
import { Bar, Line } from "react-chartjs-2";
import { Text, Card, Flex, Tag } from "@nvidia/foundations-react-core";
import { textColorForYlGnNorm, ylGnColor } from "../lib/ylGnHeatmap";
import { apiFetch } from "../lib/apiFetch";

ChartJS.register(
  CategoryScale,
  LinearScale,
  BarElement,
  LineElement,
  PointElement,
  Title,
  Tooltip,
  Legend,
  Filler,
);

export interface ClusterkitRunRow {
  dir: string;
  rel_path?: string;
  mtime?: number;
  meta?: Record<string, unknown>;
  summary?: Record<string, unknown>;
  error?: string;
  /** Present when ``latency.json`` exists beside ``bandwidth.json`` in the run directory. */
  has_latency?: boolean;
}

export interface ClusterkitLatencyDetail {
  labels: string[];
  matrix: (number | null)[][];
  meta: Record<string, unknown>;
  summary: Record<string, unknown>;
}

interface ClusterkitListResponse {
  ok?: boolean;
  error?: string;
  rack?: string;
  runs?: ClusterkitRunRow[];
}

interface ClusterkitDetailPayload {
  dir: string;
  labels: string[];
  matrix: (number | null)[][];
  meta: Record<string, unknown>;
  summary: Record<string, unknown>;
  latency?: ClusterkitLatencyDetail | null;
}

interface ClusterkitDetailResponse extends ClusterkitListResponse {
  detail?: ClusterkitDetailPayload;
}

function metaStr(v: unknown): string {
  if (v == null) return "";
  const s = String(v).trim();
  return s;
}

function clusterkitMatrixLimits(matrix: (number | null)[][]): { vmin: number; vmax: number } {
  const flat: number[] = [];
  for (let i = 0; i < matrix.length; i++) {
    const row = matrix[i] || [];
    for (let j = 0; j < row.length; j++) {
      if (i === j) continue;
      const v = row[j];
      if (typeof v === "number" && Number.isFinite(v) && v > 0) flat.push(v);
    }
  }
  const vmin = 0;
  if (!flat.length) return { vmin, vmax: 1 };
  const vmax = Math.max(...flat);
  return { vmin, vmax: vmax > vmin ? vmax : vmin + 1 };
}

function offDiagonalPositiveValues(matrix: (number | null)[][]): number[] {
  const out: number[] = [];
  for (let i = 0; i < matrix.length; i++) {
    const row = matrix[i] || [];
    for (let j = 0; j < row.length; j++) {
      if (i === j) continue;
      const v = row[j];
      if (typeof v === "number" && Number.isFinite(v) && v > 0) out.push(v);
    }
  }
  return out;
}

/** Min/max over positive off-diagonal cells (for latency coloring: best link = vmin). */
function clusterkitPositiveOffdiagMinMax(matrix: (number | null)[][]): { vmin: number; vmax: number } {
  const flat = offDiagonalPositiveValues(matrix);
  if (!flat.length) return { vmin: 0, vmax: 1 };
  const vmin = Math.min(...flat);
  const vmax = Math.max(...flat);
  return vmax > vmin ? { vmin, vmax } : { vmin, vmax: vmin + 1e-9 };
}

/** Higher value = lower YlGn norm (lighter); lower latency = darker green (better). */
function latencyGoodnessNorm(value: number, vmin: number, vmax: number): number {
  if (!(vmax > vmin) || !Number.isFinite(value)) return 0.5;
  return Math.max(0, Math.min(1, (vmax - value) / (vmax - vmin)));
}

function formatClusterkitCell(v: number | null, units: string): string {
  if (v == null || !Number.isFinite(v)) return "—";
  if (v === 0) return "0";
  const u = units.toLowerCase();
  if (u.includes("gb") || u.includes("tb")) return v.toFixed(2);
  if (Math.abs(v - Math.round(v)) < 1e-6) return String(Math.round(v));
  return v.toFixed(1);
}

function formatLatencyCell(v: number | null, units: string): string {
  if (v == null || !Number.isFinite(v)) return "—";
  if (v === 0) return "0";
  void units;
  return v.toFixed(3);
}

function buildLatencyHistogram(
  values: number[],
  binCount: number,
): { labels: string[]; counts: number[]; ranges: string[] } | null {
  if (values.length < 1) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const lo = sorted[0];
  const hi = sorted[sorted.length - 1];
  if (!(hi > lo)) {
    return {
      labels: ["1"],
      counts: [values.length],
      ranges: [`${lo.toFixed(4)} (all links)`],
    };
  }
  const bins = Math.min(binCount, Math.max(4, values.length));
  const width = (hi - lo) / bins;
  const counts = new Array(bins).fill(0) as number[];
  for (const v of values) {
    let i = Math.floor((v - lo) / width);
    if (i >= bins) i = bins - 1;
    if (i < 0) i = 0;
    counts[i]++;
  }
  const ranges = counts.map((_, i) => {
    const a = lo + i * width;
    const b = i === bins - 1 ? hi : lo + (i + 1) * width;
    return `${a.toFixed(4)} – ${b.toFixed(4)}`;
  });
  const labels = counts.map((_, i) => String(i + 1));
  return { labels, counts, ranges };
}

function ClusterkitLatencyCharts({ matrix, latUnits }: { matrix: (number | null)[][]; latUnits: string }) {
  const histModel = useMemo(() => {
    const values = offDiagonalPositiveValues(matrix);
    if (values.length < 2) return null;
    const bins = Math.min(16, Math.max(6, Math.ceil(Math.sqrt(values.length))));
    const built = buildLatencyHistogram(values, bins);
    if (!built) return null;
    return { ...built };
  }, [matrix]);

  const sortedPts = useMemo(() => {
    const values = offDiagonalPositiveValues(matrix);
    if (values.length < 2) return null;
    const sorted = [...values].sort((a, b) => a - b);
    return sorted.map((y, x) => ({ x, y }));
  }, [matrix]);

  if (!histModel || !sortedPts) {
    return (
      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
        Not enough pairwise latency samples for charts (need at least two link measurements).
      </Text>
    );
  }

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 3, minWidth: 0 }}>
      <Box>
        <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 8 }}>
          Latency histogram (link count per bin)
        </Text>
        <Box sx={{ position: "relative", width: "100%", minWidth: 0, height: 220, minHeight: 220 }}>
          <Bar
            data={{
              labels: histModel.labels,
              datasets: [
                {
                  label: "Links",
                  data: histModel.counts,
                  backgroundColor: "rgba(107, 140, 255, 0.72)",
                  borderColor: "rgba(60, 90, 200, 0.9)",
                  borderWidth: 1,
                },
              ],
            }}
            options={{
              responsive: true,
              maintainAspectRatio: false,
              plugins: {
                legend: { display: false },
                tooltip: {
                  callbacks: {
                    title: (items) => {
                      const idx = items[0]?.dataIndex ?? 0;
                      return histModel.ranges[idx] ?? "";
                    },
                    label: (ctx) => `${ctx.parsed.y ?? 0} links`,
                  },
                },
              },
              scales: {
                x: {
                  title: { display: true, text: `Bin (1–${histModel.labels.length}) · ${latUnits}` },
                  ticks: { maxRotation: 0 },
                },
                y: {
                  beginAtZero: true,
                  title: { display: true, text: "Count" },
                  ticks: { precision: 0 },
                },
              },
            }}
          />
        </Box>
      </Box>
      <Box>
        <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 8 }}>
          Sorted link latencies (rank order)
        </Text>
        <Text kind="body/regular/xs" style={{ color: "var(--text-color-secondary)", marginBottom: 6, display: "block" }}>
          X-axis is link rank after sorting; Y-axis is latency ({latUnits}). Spikes on the right are slower links.
        </Text>
        <Box sx={{ position: "relative", width: "100%", minWidth: 0, height: 240, minHeight: 240 }}>
          <Line
            data={{
              datasets: [
                {
                  label: `Latency (${latUnits})`,
                  data: sortedPts,
                  borderColor: "rgba(255, 140, 66, 0.95)",
                  backgroundColor: "rgba(255, 140, 66, 0.08)",
                  borderWidth: 2,
                  pointRadius: 0,
                  pointHoverRadius: 4,
                  fill: true,
                  tension: 0.15,
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
                    label: (ctx) => {
                      const y = (ctx.parsed as { y?: number }).y;
                      return `rank ${(ctx.parsed as { x?: number }).x}: ${Number(y ?? 0).toFixed(4)} ${latUnits}`;
                    },
                  },
                },
              },
              scales: {
                x: {
                  type: "linear",
                  title: { display: true, text: "Sorted rank" },
                  ticks: { maxTicksLimit: 14 },
                },
                y: {
                  title: { display: true, text: latUnits },
                  beginAtZero: false,
                },
              },
            }}
          />
        </Box>
      </Box>
    </Box>
  );
}

export interface ClusterkitResultsSectionProps {
  logDir: string;
  rack: string;
  runs: ClusterkitRunRow[];
  listLoading: boolean;
  listError: string;
}

export function ClusterkitResultsSection({ logDir, rack, runs, listLoading, listError }: ClusterkitResultsSectionProps) {
  const runnableRuns = useMemo(() => runs.filter((r) => r.dir && !r.error), [runs]);
  const [selectedDir, setSelectedDir] = useState("");
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [detail, setDetail] = useState<ClusterkitDetailPayload | null>(null);

  useEffect(() => {
    if (!runs.length) {
      setSelectedDir("");
      setDetail(null);
      return;
    }
    setSelectedDir((cur) => {
      const pool = runnableRuns.length ? runnableRuns : runs;
      if (cur && pool.some((r) => r.dir === cur)) return cur;
      return pool[0].dir;
    });
  }, [runs, runnableRuns]);

  useEffect(() => {
    if (!logDir.trim() || !rack.trim() || !selectedDir) {
      setDetail(null);
      setDetailError("");
      setDetailLoading(false);
      return;
    }
    const ac = new AbortController();
    setDetailLoading(true);
    setDetailError("");
    const params = new URLSearchParams();
    params.set("log_dir", logDir.trim());
    params.set("rack", rack.trim());
    params.set("run", selectedDir);
    apiFetch(`/api/clusterkit-results?${params}`, { signal: ac.signal })
      .then(async (res) => {
        const j = (await res.json()) as ClusterkitDetailResponse;
        if (!res.ok || !j.ok) {
          setDetail(null);
          setDetailError(j.error || res.statusText || "ClusterKit detail request failed");
          return;
        }
        if (j.detail && Array.isArray(j.detail.labels) && Array.isArray(j.detail.matrix)) {
          setDetail(j.detail);
          setDetailError("");
        } else {
          setDetail(null);
          setDetailError("No matrix in response (run may be invalid or bandwidth.json unparsable).");
        }
      })
      .catch((err: Error) => {
        if (err.name === "AbortError") return;
        setDetail(null);
        setDetailError(err.message);
      })
      .finally(() => {
        if (!ac.signal.aborted) setDetailLoading(false);
      });
    return () => ac.abort();
  }, [logDir, rack, selectedDir]);

  if (listLoading) {
    return (
      <Box sx={{ display: "flex", alignItems: "center", gap: 2, py: 2 }}>
        <CircularProgress size={22} />
        <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
          Loading ClusterKit runs…
        </Text>
      </Box>
    );
  }
  if (listError) {
    return <Text kind="body/regular/md">{listError}</Text>;
  }
  if (!runs.length) {
    return (
      <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
        No ClusterKit results under{" "}
        <span style={{ fontFamily: "monospace" }}>log_dir/clusterkit/{rack}/</span> (no run directories with
        bandwidth.json).
      </Text>
    );
  }

  const units = metaStr(detail?.meta?.units) || "MB/s";
  const { vmin, vmax } = detail?.matrix ? clusterkitMatrixLimits(detail.matrix) : { vmin: 0, vmax: 1 };
  const n = detail?.matrix.length ?? 0;
  const fontPx = Math.max(9, Math.min(13, Math.round(64 / Math.max(n, 6))));
  const thin = "1px solid rgba(255,255,255,0.35)";

  const lat = detail?.latency;
  const latUnits = lat ? metaStr(lat.meta?.units) || "usec" : "usec";
  const latLimits = lat?.matrix ? clusterkitPositiveOffdiagMinMax(lat.matrix) : { vmin: 0, vmax: 1 };
  const latVmin = latLimits.vmin;
  const latVmax = latLimits.vmax;
  const nLat = lat?.matrix.length ?? 0;
  const fontPxLat = Math.max(9, Math.min(13, Math.round(64 / Math.max(nLat, 6))));

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}>
      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
        ClusterKit pairwise results per run: <span style={{ fontFamily: "monospace" }}>bandwidth.json</span> and, when
        present, <span style={{ fontFamily: "monospace" }}>latency.json</span> (same node layout as upstream{" "}
        <span style={{ fontFamily: "monospace" }}>bwResultAnalyzer</span>).
      </Text>

      {runs.some((r) => r.error) && (
        <BannerSoft>
          Some runs could not be read:{" "}
          {runs
            .filter((r) => r.error)
            .map((r) => `${r.dir}: ${r.error}`)
            .join(" · ")}
        </BannerSoft>
      )}

      <FormControl size="small" sx={{ maxWidth: 420 }}>
        <InputLabel id="clusterkit-run-label">Run</InputLabel>
        <Select
          labelId="clusterkit-run-label"
          label="Run"
          value={selectedDir && runs.some((r) => r.dir === selectedDir) ? selectedDir : ""}
          onChange={(e) => setSelectedDir(String(e.target.value))}
        >
          {runs.map((r) => (
            <MenuItem key={r.dir} value={r.dir} disabled={!!r.error}>
              {r.dir}
              {r.error
                ? ` — ${r.error}`
                : `${r.meta?.hca_tag != null ? ` (${metaStr(r.meta.hca_tag)})` : ""}${r.has_latency ? " · latency" : ""}`}
            </MenuItem>
          ))}
        </Select>
      </FormControl>

      {detailLoading && (
        <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
          <CircularProgress size={20} />
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            Loading run detail…
          </Text>
        </Box>
      )}
      {detailError && !detailLoading && <Text kind="body/regular/md">{detailError}</Text>}

      {detail && !detailLoading && (
        <>
          <Card style={{ padding: 16, maxWidth: "100%", boxSizing: "border-box", overflow: "hidden" }}>
            <Text kind="body/bold/md" style={{ display: "block", marginBottom: 8 }}>
              Inter-node bandwidth
            </Text>
            <Flex gap="2" wrap="wrap" style={{ marginBottom: 12 }}>
              {metaStr(detail.meta.cluster) && (
                <Tag color="gray" kind="outline" readOnly density="compact">
                  {metaStr(detail.meta.cluster)}
                </Tag>
              )}
              {metaStr(detail.meta.hca_tag) && (
                <Tag color="teal" kind="outline" readOnly density="compact">
                  HCA {metaStr(detail.meta.hca_tag)}
                </Tag>
              )}
              {metaStr(detail.meta.jobid) && (
                <Tag color="gray" kind="outline" readOnly density="compact">
                  JOB {metaStr(detail.meta.jobid)}
                </Tag>
              )}
              {metaStr(detail.meta.bidirectional) && (
                <Tag color="gray" kind="outline" readOnly density="compact">
                  {metaStr(detail.meta.bidirectional)}
                </Tag>
              )}
              <Tag color="green" kind="outline" readOnly density="compact">
                {units}
              </Tag>
            </Flex>
            {(metaStr(detail.meta.date_and_time) || metaStr(detail.meta.testname)) && (
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 12, display: "block" }}>
                {metaStr(detail.meta.testname) && <span>{metaStr(detail.meta.testname)} · </span>}
                {metaStr(detail.meta.date_and_time)}
              </Text>
            )}

            <Box sx={{ display: "flex", flexDirection: { xs: "column", md: "row" }, gap: 2, alignItems: "stretch", minWidth: 0 }}>
              <Box sx={{ flex: "1 1 0%", minWidth: 0, overflowX: "auto" }}>
                <table
                  style={{
                    borderCollapse: "collapse",
                    fontSize: fontPx,
                    fontFamily: "monospace",
                    width: "max-content",
                  }}
                >
                  <thead>
                    <tr>
                      <th style={{ padding: 4 }} />
                      {detail.labels.map((lx, j) => (
                        <th key={`h-${j}`} style={{ padding: "4px 6px", fontWeight: 600, textAlign: "center" }}>
                          {lx}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {detail.matrix.map((row, i) => (
                      <tr key={`r-${i}`}>
                        <td
                          style={{
                            padding: "4px 8px 4px 0",
                            textAlign: "right",
                            whiteSpace: "nowrap",
                            color: "var(--text-color-secondary)",
                            verticalAlign: "middle",
                          }}
                        >
                          {detail.labels[i] ?? String(i)}
                        </td>
                        {row.map((v, j) => {
                          const nan = v == null || !Number.isFinite(v);
                          const num = nan ? NaN : (v as number);
                          const isDiag = i === j;
                          const norm = nan || isDiag ? 0 : (num - vmin) / (vmax - vmin || 1);
                          const bg = nan ? "rgba(128,128,128,0.25)" : isDiag ? "rgba(128,128,128,0.15)" : ylGnColor(norm);
                          return (
                            <td
                              key={`c-${i}-${j}`}
                              style={{
                                borderLeft: thin,
                                borderTop: thin,
                                backgroundColor: bg,
                                color: nan || isDiag ? "var(--text-color-secondary)" : textColorForYlGnNorm(norm),
                                textAlign: "center",
                                minWidth: 40,
                                padding: "5px 3px",
                                fontWeight: 500,
                              }}
                            >
                              {nan ? "—" : formatClusterkitCell(num, units)}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Box>
              <Box
                sx={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  gap: 1,
                  width: 56,
                  flexShrink: 0,
                  alignSelf: { xs: "center", md: "stretch" },
                }}
              >
                <Text kind="body/regular/xs" style={{ color: "var(--text-color-secondary)" }}>
                  {units}
                </Text>
                <Box
                  sx={{
                    width: 22,
                    flex: 1,
                    minHeight: 120,
                    borderRadius: 1,
                    border: "1px solid rgba(0,0,0,0.2)",
                    background: "linear-gradient(to top, rgb(0,104,55), rgb(173,221,142), rgb(255,255,229))",
                  }}
                />
                <Text kind="body/regular/xs" style={{ fontFamily: "monospace" }}>
                  {vmax >= 100 ? Math.round(vmax) : vmax.toFixed(1)}
                </Text>
                <Text kind="body/regular/xs" style={{ fontFamily: "monospace" }}>
                  {vmin}
                </Text>
              </Box>
            </Box>

            {detail.summary && (detail.summary.min != null || detail.summary.avg != null || detail.summary.max != null) && (
              <Box sx={{ marginTop: 2 }}>
                <Text kind="body/bold/sm" style={{ marginBottom: 6 }}>
                  Off-diagonal links
                </Text>
                <Box sx={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 1, maxWidth: 360 }}>
                  {(["min", "avg", "max"] as const).map((k) => {
                    const raw = detail.summary[k];
                    const val = typeof raw === "number" ? raw : parseFloat(String(raw ?? ""));
                    if (!Number.isFinite(val)) {
                      return (
                        <Box key={k} sx={{ textAlign: "center", py: 1, color: "var(--text-color-secondary)" }}>
                          <Text kind="body/regular/xs">{k}</Text>
                          —
                        </Box>
                      );
                    }
                    const norm = (val - vmin) / (vmax - vmin || 1);
                    return (
                      <Box
                        key={k}
                        sx={{
                          textAlign: "center",
                          py: 1.5,
                          px: 1,
                          borderRadius: 1,
                          backgroundColor: ylGnColor(norm),
                          color: textColorForYlGnNorm(norm),
                          fontFamily: "monospace",
                          fontWeight: 600,
                          fontSize: 14,
                        }}
                      >
                        <Text kind="body/regular/xs" style={{ opacity: 0.9, display: "block", marginBottom: 4 }}>
                          {k}
                        </Text>
                        {k === "avg" ? val.toFixed(1) : Math.round(val).toString()} {units}
                      </Box>
                    );
                  })}
                </Box>
                {(detail.summary.node_count != null || detail.summary.link_count != null) && (
                  <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginTop: 8, display: "block" }}>
                    {detail.summary.node_count != null && <>Nodes: {String(detail.summary.node_count)} </>}
                    {detail.summary.link_count != null && <>· Measured links: {String(detail.summary.link_count)}</>}
                  </Text>
                )}
              </Box>
            )}
          </Card>

          {!lat && (
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              No <span style={{ fontFamily: "monospace" }}>latency.json</span> next to this run’s bandwidth file (latency
              heatmap and charts omitted).
            </Text>
          )}

          {lat && lat.matrix?.length > 0 && (
            <>
              <Card style={{ padding: 16, maxWidth: "100%", boxSizing: "border-box", overflow: "hidden" }}>
                <Text kind="body/bold/md" style={{ display: "block", marginBottom: 8 }}>
                  Inter-node latency
                </Text>
                <Text kind="body/regular/xs" style={{ color: "var(--text-color-secondary)", marginBottom: 10, display: "block" }}>
                  Darker green is lower latency (better). Scale matches the slowest link in this matrix.
                </Text>
                <Flex gap="2" wrap="wrap" style={{ marginBottom: 12 }}>
                  {metaStr(lat.meta.cluster) && (
                    <Tag color="gray" kind="outline" readOnly density="compact">
                      {metaStr(lat.meta.cluster)}
                    </Tag>
                  )}
                  {metaStr(lat.meta.hca_tag) && (
                    <Tag color="teal" kind="outline" readOnly density="compact">
                      HCA {metaStr(lat.meta.hca_tag)}
                    </Tag>
                  )}
                  {metaStr(lat.meta.jobid) && (
                    <Tag color="gray" kind="outline" readOnly density="compact">
                      JOB {metaStr(lat.meta.jobid)}
                    </Tag>
                  )}
                  <Tag color="green" kind="outline" readOnly density="compact">
                    {latUnits}
                  </Tag>
                </Flex>
                {(metaStr(lat.meta.date_and_time) || metaStr(lat.meta.testname)) && (
                  <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 12, display: "block" }}>
                    {metaStr(lat.meta.testname) && <span>{metaStr(lat.meta.testname)} · </span>}
                    {metaStr(lat.meta.date_and_time)}
                  </Text>
                )}

                <Box sx={{ display: "flex", flexDirection: { xs: "column", md: "row" }, gap: 2, alignItems: "stretch", minWidth: 0 }}>
                  <Box sx={{ flex: "1 1 0%", minWidth: 0, overflowX: "auto" }}>
                    <table
                      style={{
                        borderCollapse: "collapse",
                        fontSize: fontPxLat,
                        fontFamily: "monospace",
                        width: "max-content",
                      }}
                    >
                      <thead>
                        <tr>
                          <th style={{ padding: 4 }} />
                          {lat.labels.map((lx, j) => (
                            <th key={`lh-${j}`} style={{ padding: "4px 6px", fontWeight: 600, textAlign: "center" }}>
                              {lx}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {lat.matrix.map((row, i) => (
                          <tr key={`lr-${i}`}>
                            <td
                              style={{
                                padding: "4px 8px 4px 0",
                                textAlign: "right",
                                whiteSpace: "nowrap",
                                color: "var(--text-color-secondary)",
                                verticalAlign: "middle",
                              }}
                            >
                              {lat.labels[i] ?? String(i)}
                            </td>
                            {row.map((v, j) => {
                              const nan = v == null || !Number.isFinite(v);
                              const num = nan ? NaN : (v as number);
                              const isDiag = i === j;
                              const g = nan || isDiag ? 0.5 : latencyGoodnessNorm(num, latVmin, latVmax);
                              const bg = nan
                                ? "rgba(128,128,128,0.25)"
                                : isDiag
                                  ? "rgba(128,128,128,0.15)"
                                  : ylGnColor(g);
                              return (
                                <td
                                  key={`lc-${i}-${j}`}
                                  style={{
                                    borderLeft: thin,
                                    borderTop: thin,
                                    backgroundColor: bg,
                                    color: nan || isDiag ? "var(--text-color-secondary)" : textColorForYlGnNorm(g),
                                    textAlign: "center",
                                    minWidth: 40,
                                    padding: "5px 3px",
                                    fontWeight: 500,
                                  }}
                                >
                                  {nan ? "—" : formatLatencyCell(num, latUnits)}
                                </td>
                              );
                            })}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </Box>
                  <Box
                    sx={{
                      display: "flex",
                      flexDirection: "column",
                      alignItems: "center",
                      gap: 1,
                      width: 56,
                      flexShrink: 0,
                      alignSelf: { xs: "center", md: "stretch" },
                    }}
                  >
                    <Text kind="body/regular/xs" style={{ color: "var(--text-color-secondary)" }}>
                      {latUnits}
                    </Text>
                    <Box
                      sx={{
                        width: 22,
                        flex: 1,
                        minHeight: 120,
                        borderRadius: 1,
                        border: "1px solid rgba(0,0,0,0.2)",
                        background: "linear-gradient(to bottom, rgb(255,255,229), rgb(173,221,142), rgb(0,104,55))",
                      }}
                    />
                    <Text kind="body/regular/xs" style={{ fontFamily: "monospace" }}>
                      {latVmax >= 100 ? Math.round(latVmax) : latVmax.toFixed(3)}
                    </Text>
                    <Text kind="body/regular/xs" style={{ fontFamily: "monospace" }}>
                      {latVmin}
                    </Text>
                  </Box>
                </Box>

                {lat.summary && (lat.summary.min != null || lat.summary.avg != null || lat.summary.max != null) && (
                  <Box sx={{ marginTop: 2 }}>
                    <Text kind="body/bold/sm" style={{ marginBottom: 6 }}>
                      Off-diagonal links
                    </Text>
                    <Box sx={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 1, maxWidth: 360 }}>
                      {(["min", "avg", "max"] as const).map((k) => {
                        const raw = lat.summary[k];
                        const val = typeof raw === "number" ? raw : parseFloat(String(raw ?? ""));
                        if (!Number.isFinite(val)) {
                          return (
                            <Box key={k} sx={{ textAlign: "center", py: 1, color: "var(--text-color-secondary)" }}>
                              <Text kind="body/regular/xs">{k}</Text>
                              —
                            </Box>
                          );
                        }
                        const g = latencyGoodnessNorm(val, latVmin, latVmax);
                        return (
                          <Box
                            key={k}
                            sx={{
                              textAlign: "center",
                              py: 1.5,
                              px: 1,
                              borderRadius: 1,
                              backgroundColor: ylGnColor(g),
                              color: textColorForYlGnNorm(g),
                              fontFamily: "monospace",
                              fontWeight: 600,
                              fontSize: 14,
                            }}
                          >
                            <Text kind="body/regular/xs" style={{ opacity: 0.9, display: "block", marginBottom: 4 }}>
                              {k}
                            </Text>
                            {val.toFixed(3)} {latUnits}
                          </Box>
                        );
                      })}
                    </Box>
                  </Box>
                )}
              </Card>

              <Card style={{ padding: 16, maxWidth: "100%", boxSizing: "border-box", overflow: "visible" }}>
                <Text kind="body/bold/md" style={{ display: "block", marginBottom: 8 }}>
                  Latency charts
                </Text>
                <ClusterkitLatencyCharts matrix={lat.matrix} latUnits={latUnits} />
              </Card>
            </>
          )}
        </>
      )}
    </Box>
  );
}

function BannerSoft({ children }: { children: ReactNode }) {
  return (
    <Card style={{ padding: 12, backgroundColor: "var(--background-color-surface-raised, #2a2418)" }}>
      <Text kind="body/regular/sm">{children}</Text>
    </Card>
  );
}
