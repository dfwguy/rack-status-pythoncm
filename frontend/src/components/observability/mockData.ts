/**
 * Synthetic observability data shared by the Metrics / Logs / Events preview views.
 * All values are generated — these screens are design mockups (see PreviewBanner)
 * until wired to Grafana, parser logs, and the command/lock/Slurm event sources.
 */

// ---- Topology (cluster → rack → node) --------------------------------------

export interface ScopeNode {
  /** Host id, also used as the selection key and to match log/event hosts. */
  id: string;
  label: string;
  /** Unreachable / powered-off nodes render disabled and cannot be selected. */
  available: boolean;
}

export interface ScopeRack {
  id: string;
  label: string;
  nodes: ScopeNode[];
}

export interface ScopeCluster {
  id: string;
  label: string;
  racks: ScopeRack[];
}

export const CLUSTER_TOPOLOGY: ScopeCluster = {
  id: "cluster",
  label: "GB300 NVL72 — site A",
  racks: [
    {
      id: "rack-03",
      label: "Rack 03",
      nodes: [
        { id: "ct03-01", label: "ct03-01", available: true },
        { id: "ct03-02", label: "ct03-02", available: true },
        { id: "ct03-03", label: "ct03-03", available: false },
      ],
    },
    {
      id: "rack-07",
      label: "Rack 07",
      nodes: [
        { id: "ct07-01", label: "ct07-01", available: true },
        { id: "ct07-02", label: "ct07-02", available: true },
        { id: "ct07-03", label: "ct07-03", available: true },
        { id: "ct07-04", label: "ct07-04", available: true },
        { id: "ct07-05", label: "ct07-05", available: true },
        { id: "ct07-06", label: "ct07-06", available: true },
      ],
    },
  ],
};

/** All selectable (available) host ids across the topology. */
export function allAvailableHostIds(topology: ScopeCluster): string[] {
  return topology.racks.flatMap((r) => r.nodes.filter((n) => n.available).map((n) => n.id));
}

/** Human summary of the current selection, e.g. "Rack 07 · 6 nodes" or "9 nodes across 2 racks". */
export function selectionSummary(topology: ScopeCluster, selected: Set<string>): string {
  const count = selected.size;
  if (count === 0) return "No nodes selected";
  const racksTouched = topology.racks.filter((r) => r.nodes.some((n) => selected.has(n.id)));
  const fullRacks = racksTouched.filter((r) =>
    r.nodes.filter((n) => n.available).every((n) => selected.has(n.id))
  );
  if (racksTouched.length === 1) {
    const r = racksTouched[0];
    const label = fullRacks.length === 1 ? r.label : `${r.label} (partial)`;
    return `${label} · ${count} node${count === 1 ? "" : "s"}`;
  }
  return `${count} nodes across ${racksTouched.length} racks`;
}

// ---- Metrics ----------------------------------------------------------------

export interface MetricPanel {
  id: string;
  title: string;
  unit: string;
  color: string;
  base: number;
  amplitude: number;
}

export const METRIC_PANELS: MetricPanel[] = [
  { id: "gpu-temp", title: "GPU temperature", unit: "°C", color: "#f2994a", base: 64, amplitude: 6 },
  { id: "gpu-power", title: "GPU power draw", unit: "W", color: "#76B900", base: 920, amplitude: 110 },
  { id: "nvlink-bw", title: "NVLink bandwidth", unit: "GB/s", color: "#2f9bff", base: 540, amplitude: 70 },
  { id: "ib-throughput", title: "InfiniBand throughput", unit: "Gb/s", color: "#9b6dff", base: 360, amplitude: 50 },
];

/** Deterministic-ish series so the mock doesn't flicker on re-render. */
export function makeSeries(base: number, amplitude: number, points: number, phase = 0): number[] {
  return Array.from({ length: points }, (_, i) => {
    const wave = Math.sin(i / 5 + phase) * amplitude;
    const drift = Math.cos(i / 11 + phase) * amplitude * 0.4;
    return Math.round((base + wave + drift) * 10) / 10;
  });
}

export function timeLabels(points: number): string[] {
  const now = Date.now();
  return Array.from({ length: points }, (_, i) => {
    const d = new Date(now - (points - 1 - i) * 30_000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  });
}

// ---- Logs -------------------------------------------------------------------

export type LogLevel = "info" | "warn" | "error";

export interface LogLine {
  ts: string;
  level: LogLevel;
  host: string;
  msg: string;
}

export const MOCK_LOGS: LogLine[] = [
  { ts: "14:32:08", level: "info", host: "ct07-01", msg: "nccl-tests all_reduce_perf: busbw 478.2 GB/s (pass)" },
  { ts: "14:31:55", level: "info", host: "ct07-02", msg: "DCGM health check OK — 8/8 GPUs nominal" },
  { ts: "14:31:40", level: "warn", host: "ct07-05", msg: "GPU3 temp 78C approaching soft limit (80C)" },
  { ts: "14:30:12", level: "info", host: "ct07-03", msg: "nemo pretrain step 1450 loss=2.13 tokens/s=18.4k" },
  { ts: "14:29:47", level: "error", host: "ct07-06", msg: "ib link mlx5_4 reported 1 symbol error in last interval" },
  { ts: "14:28:30", level: "info", host: "ct07-01", msg: "validation suite started: nccl,nemo,clusterkit" },
  { ts: "14:27:02", level: "info", host: "ct03-02", msg: "clusterkit bandwidth sweep complete (p2p 742 GB/s)" },
  { ts: "14:25:48", level: "warn", host: "ct03-01", msg: "nvlink retrain event on link 6 — recovered in 1.2s" },
  { ts: "14:24:11", level: "info", host: "ct07-04", msg: "hpcx module loaded; osu_bw 24.1 GB/s" },
  { ts: "14:22:53", level: "error", host: "ct03-01", msg: "nemo dataloader stall: NFS read 9.4s on /datasets" },
];

// ---- Events -----------------------------------------------------------------

export type EventKind = "drain" | "job" | "power" | "lock" | "alert";

export const EVENT_META: Record<EventKind, { color: "green" | "red" | "yellow" | "blue"; label: string }> = {
  drain: { color: "yellow", label: "Drain" },
  job: { color: "green", label: "Job" },
  power: { color: "blue", label: "Power" },
  lock: { color: "blue", label: "Lock" },
  alert: { color: "red", label: "Alert" },
};

export interface EventItem {
  ts: string;
  kind: EventKind;
  host: string;
  text: string;
}

export const MOCK_EVENTS: EventItem[] = [
  { ts: "14:32:01", kind: "job", host: "ct07-01", text: "NEMO pretrain completed on Rack 07 (exit 0)" },
  { ts: "14:30:18", kind: "alert", host: "ct07-06", text: "IB symbol errors on ct07-06 mlx5_4 exceeded threshold" },
  { ts: "14:27:44", kind: "drain", host: "ct07-05", text: "ct07-05 drained for thermal investigation by rkoike" },
  { ts: "14:25:48", kind: "alert", host: "ct03-01", text: "NVLink retrain on ct03-01 link 6 (auto-recovered)" },
  { ts: "14:21:09", kind: "lock", host: "ct07-01", text: "Rack 07 claimed by rkoike (validation campaign)" },
  { ts: "14:12:30", kind: "job", host: "ct03-02", text: "ClusterKit sweep started on Rack 03" },
  { ts: "14:05:33", kind: "power", host: "ct07-01", text: "Rack 07 power cycle completed — all trays back online" },
  { ts: "13:58:14", kind: "drain", host: "ct03-01", text: "ct03-01 undrained after NFS mount recovery" },
];
