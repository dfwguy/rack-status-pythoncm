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
/**
 * Observability → Metrics.
 *
 * Renders charts NATIVELY with Chart.js from our own metrics API
 * (`GET /api/metrics/catalog` + `/api/metrics/chart/<id>`), which reads VictoriaMetrics.
 *
 * This replaces the previous Grafana `<iframe>` embeds. Iframes cost a second tunnel, a second
 * login, a Grafana `allow_embedding` relaxation, and rendered as a page-inside-a-page. Charting
 * locally removes all of that and matches the rest of the app (AnalyticsView already uses Chart.js).
 * Grafana stays available as a deep-link when someone wants the full dashboard.
 *
 * Honesty: a chart with no series renders an explicit "no data" state — never a flat zero line,
 * never generated numbers. As of 2026-07-28 DCGM reports ~230 hosts so most GPU charts DO have
 * data; fabric charts stay empty while the switch scrapers are stopped for the incident.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Box from "@mui/material/Box";
import MenuItem from "@mui/material/MenuItem";
import TextField from "@mui/material/TextField";
import { Banner, Card, Flex, Skeleton, StatusMessage, Text } from "@nvidia/foundations-react-core";
import { Activity, Unplug } from "lucide-react";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  BarElement,
  Title,
  Tooltip,
  Legend,
  Filler,
} from "chart.js";
import { Line, Bar } from "react-chartjs-2";
import { PageHeader } from "../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { useThemeMode } from "../context/ThemeContext";
import { apiFetch } from "../lib/apiFetch";
import { compareMetricsRacks, metricsRackMoniker } from "../lib/metricsRack";

ChartJS.register(
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  BarElement,
  Title,
  Tooltip,
  Legend,
  Filler,
);

/* ---------------------------------------------------------------------------------------- */
/* Backend contract: backend/metrics_query.py                                                */
/* ---------------------------------------------------------------------------------------- */

interface CatalogChart {
  id: string;
  title: string;
  unit: string;
  kind: "bar" | "line";
  range: boolean;
}

interface ChartPayload {
  ok: boolean;
  error?: string;
  title?: string;
  split_by?: string;
  labels?: string[];
  datasets?: { label: string; data: (number | null)[] }[];
}

interface MetricsHealth {
  ok: boolean;
  upstream?: string;
  dcgm_hosts?: number;
  error?: string | null;
}

/** Index-stable palette so a series keeps its colour across refreshes. */
const SERIES_COLORS = ["#76b900", "#00a3e0", "#f2a900", "#c74634", "#8e7cc3", "#00b3a4"];

const WINDOWS = [
  { value: 15, label: "Last 15 min" },
  { value: 60, label: "Last 1 hour" },
  { value: 360, label: "Last 6 hours" },
  { value: 1440, label: "Last 24 hours" },
];

/** Epoch-seconds (as sent by the backend) → short local clock label for the x-axis. */
function timeLabel(epochSeconds: string): string {
  const n = Number(epochSeconds);
  if (!Number.isFinite(n)) return epochSeconds;
  return new Date(n * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function hasData(p: ChartPayload | undefined): boolean {
  if (!p?.datasets?.length) return false;
  return p.datasets.some((d) => d.data.some((v) => v !== null && v !== undefined));
}

interface MetricChartCardProps {
  chart: CatalogChart;
  rack: string;
  minutes: number;
  dark: boolean;
}

function MetricChartCard({ chart, rack, minutes, dark }: MetricChartCardProps) {
  const [payload, setPayload] = useState<ChartPayload | undefined>();
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const params = new URLSearchParams({ minutes: String(minutes) });
    if (rack) params.set("rack", rack);
    try {
      const res = await apiFetch(`/api/metrics/chart/${encodeURIComponent(chart.id)}?${params}`, {
        cache: "no-store",
      });
      setPayload(await res.json());
    } catch (e) {
      setPayload({ ok: false, error: e instanceof Error ? e.message : String(e) });
    } finally {
      setLoading(false);
    }
  }, [chart.id, rack, minutes]);

  useEffect(() => {
    void load();
    // Metrics scrape on a ~30 s cadence; refresh slower than that to stay cheap.
    const t = window.setInterval(() => void load(), 60_000);
    return () => window.clearInterval(t);
  }, [load]);

  const grid = dark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.08)";
  const tick = dark ? "#c9d1d9" : "#374151";

  const data = useMemo(
    () => ({
      labels: (payload?.labels ?? []).map((l) => (chart.range ? timeLabel(l) : l)),
      datasets: (payload?.datasets ?? []).map((d, i) => ({
        label: payload?.split_by === "rack" ? metricsRackMoniker(d.label) : d.label,
        data: d.data,
        borderColor: SERIES_COLORS[i % SERIES_COLORS.length],
        backgroundColor:
          SERIES_COLORS[i % SERIES_COLORS.length] + (chart.kind === "bar" ? "cc" : "22"),
        borderWidth: chart.kind === "bar" ? 0 : 2,
        pointRadius: 0,
        tension: 0.25,
        fill: chart.kind === "line",
        // Keep a null (absent sample) as a gap instead of bridging it, so a scrape outage stays
        // visible rather than being smoothed into a straight line.
        spanGaps: false,
      })),
    }),
    [payload, chart.kind, chart.range],
  );

  const options = useMemo(
    () => ({
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index" as const, intersect: false },
      plugins: {
        legend: { display: (payload?.datasets?.length ?? 0) > 1, labels: { color: tick } },
        tooltip: {
          callbacks: {
            label: (ctx: { dataset: { label?: string }; parsed: { y: number | null } }) =>
              `${ctx.dataset.label ?? ""}: ${ctx.parsed.y ?? "—"} ${chart.unit}`,
          },
        },
      },
      scales: {
        x: {
          grid: { color: grid },
          ticks: {
            color: tick,
            maxRotation: chart.range ? 0 : 60,
            autoSkip: true,
            maxTicksLimit: 12,
          },
        },
        y: {
          grid: { color: grid },
          ticks: { color: tick },
          title: { display: true, text: chart.unit, color: tick },
        },
      },
    }),
    [grid, tick, chart.unit, chart.range, payload],
  );

  const empty = !loading && payload?.ok && !hasData(payload);

  return (
    <Card style={{ padding: 16 }}>
      <Flex align="center" justify="between" gap="2" wrap="wrap" style={{ marginBottom: 10 }}>
        <Text kind="body/bold/sm">{payload?.title || chart.title}</Text>
        <Text kind="body/regular/xs" color="secondary">
          {chart.unit}
          {rack ? ` · ${metricsRackMoniker(rack)}` : ""}
        </Text>
      </Flex>

      {loading && !payload ? (
        <Skeleton style={{ height: 220 }} />
      ) : payload && !payload.ok ? (
        <StatusMessage
          size="small"
          slotMedia={<Unplug />}
          slotHeading="Query failed"
          slotSubheading={payload.error || "unknown error"}
        />
      ) : empty ? (
        <StatusMessage
          size="small"
          slotMedia={<Activity />}
          slotHeading="No data for this window"
          slotSubheading="The query returned no series. That is the true state — nothing is being substituted."
        />
      ) : (
        <Box sx={{ height: 240 }}>
          {chart.kind === "bar" ? (
            <Bar data={data} options={options} />
          ) : (
            <Line data={data} options={options} />
          )}
        </Box>
      )}
    </Card>
  );
}

export function MetricsView() {
  const { mode } = useThemeMode();
  const dark = mode === "dark";

  const [charts, setCharts] = useState<CatalogChart[]>([]);
  const [health, setHealth] = useState<MetricsHealth | undefined>();
  const [racks, setRacks] = useState<string[]>([]);
  const [rack, setRack] = useState("");
  const [minutes, setMinutes] = useState(60);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const [cat, hp] = await Promise.all([
          apiFetch("/api/metrics/catalog", { cache: "no-store" }).then((r) => r.json()),
          apiFetch("/api/metrics/health", { cache: "no-store" }).then((r) => r.json()),
        ]);
        if (!alive) return;
        setCharts(cat?.charts ?? []);
        setHealth(hp);
      } catch {
        if (alive) setHealth({ ok: false, error: "metrics API unreachable" });
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // Rack list comes from the metrics themselves, so the filter only offers racks that actually
  // report telemetry — no dead options carried over from a static inventory.
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const r = await apiFetch(
          `/api/metrics/query?q=${encodeURIComponent("count by (rack) (DCGM_FI_DEV_GPU_TEMP)")}`,
          { cache: "no-store" },
        ).then((x) => x.json());
        if (!alive || !r?.ok) return;
        const names: string[] = (r.result ?? [])
          .map((s: { metric?: Record<string, string> }) => s.metric?.rack)
          .filter((n: string | undefined): n is string => Boolean(n));
        setRacks([...new Set(names)].sort(compareMetricsRacks));
      } catch {
        /* the filter is optional — leave it empty on failure */
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH, mx: "auto", p: 3 }}>
      <PageHeader
        title="Metrics"
        description="Rendered with live data from Grafana/VictoriaMetrics running on this node"
      />

      {health && !health.ok ? (
        <Banner status="error" style={{ marginBottom: 16 }}>
          <Flex align="center" gap="2">
            <Unplug size={16} />
            <Text kind="body/regular/sm">
              Metrics backend unreachable{health.upstream ? ` (${health.upstream})` : ""}:{" "}
              {health.error || "unknown error"}
            </Text>
          </Flex>
        </Banner>
      ) : health?.ok ? (
        <Banner status="info" style={{ marginBottom: 16 }}>
          <Text kind="body/regular/sm">
            Live from VictoriaMetrics · <strong>{health.dcgm_hosts ?? 0}</strong> hosts reporting
            DCGM. An empty chart means no series for that window — nothing is substituted. Fabric
            metrics stay empty while the switch scrapers are stopped.
          </Text>
        </Banner>
      ) : null}

      <Flex gap="3" wrap="wrap" style={{ marginBottom: 16 }}>
        <TextField
          select
          size="small"
          label="Rack"
          value={rack}
          onChange={(e) => setRack(e.target.value)}
          sx={{ minWidth: 180 }}
        >
          <MenuItem value="">All racks</MenuItem>
          {racks.map((r) => (
            <MenuItem key={r} value={r}>
              {metricsRackMoniker(r)}
            </MenuItem>
          ))}
        </TextField>
        <TextField
          select
          size="small"
          label="Window"
          value={minutes}
          onChange={(e) => setMinutes(Number(e.target.value))}
          sx={{ minWidth: 160 }}
        >
          {WINDOWS.map((w) => (
            <MenuItem key={w.value} value={w.value}>
              {w.label}
            </MenuItem>
          ))}
        </TextField>
      </Flex>

      {loading ? (
        <Skeleton style={{ height: 300 }} />
      ) : charts.length === 0 ? (
        <StatusMessage
          size="small"
          slotMedia={<Unplug />}
          slotHeading="No charts available"
          slotSubheading="The metrics catalog is empty — check that backend/metrics_query.py is mounted and registered."
        />
      ) : (
        <Box
          sx={{
            display: "grid",
            gap: 2,
            gridTemplateColumns: { xs: "1fr", lg: "repeat(2, minmax(0, 1fr))" },
          }}
        >
          {charts.map((c) => (
            <MetricChartCard key={c.id} chart={c} rack={rack} minutes={minutes} dark={dark} />
          ))}
        </Box>
      )}
    </Box>
  );
}

export default MetricsView;
