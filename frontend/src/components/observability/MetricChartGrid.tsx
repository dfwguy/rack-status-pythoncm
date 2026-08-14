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
 * Shared native-chart grid for the Observability views.
 *
 * Backed by `backend/metrics_query.py` (VictoriaMetrics behind our own API) and drawn with
 * Chart.js — no Grafana iframe, no second tunnel, no second login. Metrics / Infrastructure /
 * Events all render the same way and differ only by the catalog `group` they request.
 *
 * Honesty rule: an empty result renders an explicit "no data" state. Never a flat zero line,
 * never substituted values — on this cluster an empty chart is usually the truth (scrapers
 * stopped for an incident, or a plane with no route).
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
import { useThemeMode } from "../../context/ThemeContext";
import { apiFetch } from "../../lib/apiFetch";
import { WINDOWS } from "../../lib/metricWindows";

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

export interface CatalogChart {
  id: string;
  title: string;
  unit: string;
  kind: "bar" | "line";
  group?: string;
  range: boolean;
}

interface ChartPayload {
  ok: boolean;
  error?: string;
  title?: string;
  labels?: string[];
  datasets?: { label: string; data: (number | null)[] }[];
  series_total?: number;
}

export interface MetricsHealth {
  ok: boolean;
  upstream?: string;
  dcgm_hosts?: number;
  error?: string | null;
}

/** Index-stable palette so a series keeps its colour across refreshes. */
const SERIES_COLORS = [
  "#76b900", "#00a3e0", "#f2a900", "#c74634", "#8e7cc3", "#00b3a4",
  "#e07b39", "#5b8def", "#c2185b", "#7cb342", "#00838f", "#8d6e63",
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

export function MetricChartCard({ chart, rack, minutes, dark }: MetricChartCardProps) {
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
        label: d.label,
        data: d.data,
        borderColor: SERIES_COLORS[i % SERIES_COLORS.length],
        backgroundColor: SERIES_COLORS[i % SERIES_COLORS.length] + "22",
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 3,
        tension: 0.25,
        fill: false,
        // Keep a null (absent sample) as a gap instead of bridging it, so a scrape outage stays
        // visible rather than being smoothed into a straight line.
        spanGaps: false,
      })),
    }),
    [payload, chart.range],
  );

  const options = useMemo(
    () => ({
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index" as const, intersect: false },
      plugins: {
        legend: {
          display: (payload?.datasets?.length ?? 0) > 1,
          position: "bottom" as const,
          labels: { color: tick, boxWidth: 12, boxHeight: 2, usePointStyle: false, padding: 10 },
        },
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
          ticks: { color: tick, autoSkip: true, maxTicksLimit: 10, maxRotation: 0 },
        },
        y: {
          grid: { color: grid },
          ticks: { color: tick },
          title: { display: true, text: chart.unit, color: tick },
        },
      },
    }),
    [grid, tick, chart.unit, payload],
  );

  const empty = !loading && payload?.ok && !hasData(payload);
  const shown = payload?.datasets?.length ?? 0;
  const total = payload?.series_total ?? shown;

  return (
    <Card style={{ padding: 16 }}>
      <Flex align="center" justify="between" gap="2" wrap="wrap" style={{ marginBottom: 10 }}>
        <Text kind="body/bold/sm">{payload?.title || chart.title}</Text>
        <Text kind="body/regular/xs" color="secondary">
          {chart.unit}
          {rack ? ` · ${rack}` : ""}
          {total > shown ? ` · showing ${shown} of ${total}` : ""}
        </Text>
      </Flex>

      {loading && !payload ? (
        <Skeleton style={{ height: 240 }} />
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
        <Box sx={{ height: 280 }}>
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

interface MetricChartGridProps {
  /** Catalog group to render: "gpu" | "infra" | "events". Omit for everything. */
  group?: string;
  /** Show the rack filter (only meaningful for per-rack GPU charts). */
  showRackFilter?: boolean;
  /** Extra note rendered inside the status banner. */
  note?: string;
}

export function MetricChartGrid({ group, showRackFilter = true, note }: MetricChartGridProps) {
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
        const q = group ? `?group=${encodeURIComponent(group)}` : "";
        const [cat, hp] = await Promise.all([
          apiFetch(`/api/metrics/catalog${q}`, { cache: "no-store" }).then((r) => r.json()),
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
  }, [group]);

  // Rack list comes from the metrics themselves, so the filter only offers racks that actually
  // report telemetry — no dead options carried over from a static inventory.
  useEffect(() => {
    if (!showRackFilter) return;
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
        setRacks(names.sort());
      } catch {
        /* the filter is optional — leave it empty on failure */
      }
    })();
    return () => {
      alive = false;
    };
  }, [showRackFilter]);

  return (
    <>
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
            DCGM. {note || "An empty chart means no series for that window — nothing is substituted."}
          </Text>
        </Banner>
      ) : null}

      <Flex gap="3" wrap="wrap" style={{ marginBottom: 16 }}>
        {showRackFilter ? (
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
                {r}
              </MenuItem>
            ))}
          </TextField>
        ) : null}
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
          slotSubheading="The metrics catalog returned nothing for this group — check that backend/metrics_query.py is mounted and registered."
        />
      ) : (
        <Box
          sx={{
            display: "grid",
            gap: 2,
            gridTemplateColumns: { xs: "1fr", xl: "repeat(2, minmax(0, 1fr))" },
          }}
        >
          {charts.map((c) => (
            <MetricChartCard
              key={c.id}
              chart={c}
              rack={showRackFilter ? rack : ""}
              minutes={minutes}
              dark={dark}
            />
          ))}
        </Box>
      )}
    </>
  );
}
