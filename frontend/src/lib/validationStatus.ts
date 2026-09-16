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
import type { BringupFleet, BringupFleetNode, BringupJob } from "../types/bringup";
import type { PostRunEvent, PostRunNode } from "../types/api";

export type ValidationStatus = "pass" | "attention" | "warning" | "not_run" | "running" | "unknown";

export interface ValidationDomain {
  id: string;
  label: string;
}

export interface ValidationPoint {
  id: string;
  domain: string;
  name: string;
  status: ValidationStatus;
  rawStatus: string;
  reason: string;
  source: "bringup" | "post-run" | "deep-audit" | "finding" | "slurm";
  timestamp: string;
  jobId: string;
  metric: string;
  metricUnit: string;
  affectsCurrent?: boolean;
  provenance?: string;
}

export interface NodeValidationState {
  status: ValidationStatus;
  running: boolean;
  points: ValidationPoint[];
  visiblePoints: ValidationPoint[];
  tooltip: string;
}

export interface SuGroup<T> {
  id: string;
  rowKey: string;
  racks: Array<{ rack: string; nodes: T[] }>;
}

export interface EtaPrediction {
  state: "complete" | "estimated" | "blocked" | "stalled" | "insufficient";
  eta: string;
  label: string;
  confidence: "high" | "medium" | "low" | "none";
  completed: number;
  total: number;
  blocked: number;
  running: number;
  samples: number;
  ratePerHour: number | null;
  methodology: string;
}

export const CORE_DOMAINS: ValidationDomain[] = [
  { id: "all", label: "All validations" },
  { id: "node-health", label: "Node Health" },
  { id: "slurm", label: "Slurm" },
  { id: "dcgm", label: "DCGM" },
  { id: "dcgm-r1", label: "DCGM diag -r1" },
  { id: "dcgm-r3", label: "DCGM diag -r3" },
  { id: "nccl", label: "NCCL" },
  { id: "nemo", label: "NeMo 56B" },
  { id: "llama", label: "Llama 3.1 405B" },
  { id: "mnubergemm", label: "MNUberGEMM" },
];

const PASS = new Set(["DONE", "PASS", "PASSED", "SUCCESS", "SUCCEEDED", "COMPLETED", "IDLE", "UP"]);
const RUNNING = new Set(["RUNNING", "ONGOING", "ALLOC", "ALLOCATED", "COMPLETING", "CONFIGURING"]);
const WARNING = new Set(["WARN", "WARNING", "DEGRADED", "STALE", "INCOMPLETE", "RETRY", "PENDING", "INCONCLUSIVE"]);
const NOT_RUN = new Set(["NOT_RUN", "NEVER", "NEVER_CHECKED"]);
const ATTENTION = new Set([
  "FAIL", "FAILED", "ERROR", "BLOCKED", "TIMEOUT", "NODE_FAIL", "DOWN", "DRAIN", "DRAINED",
  "INVALID", "BOOT_FAIL", "OUT_OF_MEMORY", "PREEMPTED", "CANCELLED", "CANCELED",
]);
const UNKNOWN = new Set(["UNKNOWN", "N_A", "NA", "MISSING", "UNREPORTED", "NO_DATA"]);
const NUMERIC_STATUS = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?$/;

/**
 * Normalize source statuses without inventing semantics for provider-specific
 * values. Numeric results are only pass evidence when the caller explicitly
 * says the source contract treats a metric value as completion evidence.
 */
export function validationStatus(raw: string, allowNumericMetric = false): ValidationStatus {
  const trimmed = String(raw || "")
    .trim()
    .toUpperCase()
    .replace(/\*+$/, "")
    .trim();
  if (allowNumericMetric && NUMERIC_STATUS.test(trimmed)) return "pass";
  const normalized = trimmed.replace(/[\s-]+/g, "_");
  if (!normalized || UNKNOWN.has(normalized)) return "unknown";
  if (NOT_RUN.has(normalized)) return "not_run";
  if (PASS.has(normalized)) return "pass";
  if (RUNNING.has(normalized)) return "running";
  if (WARNING.has(normalized)) return "warning";
  if (ATTENTION.has(normalized) || /^CANCEL(?:LED|ED)_BY(?:_|$)/.test(normalized)) return "attention";
  return "unknown";
}

export function validationTagTone(status: ValidationStatus): "green" | "red" | "yellow" | "gray" {
  if (status === "pass") return "green";
  if (status === "attention") return "red";
  if (status === "warning" || status === "running" || status === "not_run") return "yellow";
  return "gray";
}

/** Shared scheduler/detail color contract; raw text remains visible to users. */
export function schedulerStatusColor(raw: string): "green" | "red" | "yellow" | "gray" {
  return validationTagTone(validationStatus(raw));
}

function slug(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "other";
}

/** Classify real source names while retaining an explicit domain for every unknown job. */
export function validationDomain(name: string): ValidationDomain {
  const low = String(name || "").toLowerCase();
  if (/llm[-_ ]?pretrain[-_ ]?solo/.test(low)) return { id: "llm-pretrain-solo", label: "llm-pretrain-solo" };
  if (/llm[-_ ]?pretrain/.test(low)) return { id: "llm-pretrain", label: "llm-pretrain" };
  if (/mnubergemm|mnu[-_ ]?bergemm/.test(low)) return { id: "mnubergemm", label: "MNUberGEMM" };
  if (/dcgmi|dcgm/.test(low)) return { id: "dcgm", label: "DCGM" };
  if (/llama|405b|llmb/.test(low) && !/nccl/.test(low)) return { id: "llama", label: "Llama 3.1 405B" };
  if (/nemo|56b/.test(low)) return { id: "nemo", label: "NeMo 56B" };
  if (/nccl|allreduce|alltoall|reduce[-_ ]?scatter|forcesharp/.test(low)) return { id: "nccl", label: "NCCL" };
  if (/health|audit|sysinfo|dmesg|nvlink|pcie|memory|firmware|hardware|network/.test(low)) {
    return { id: "node-health", label: "Node Health" };
  }
  return { id: `validation:${slug(name)}`, label: name || "Other validation" };
}

export function availableValidationDomains(fleet: BringupFleet | null, nodes: PostRunNode[]): ValidationDomain[] {
  const found = new Map(CORE_DOMAINS.map((domain) => [domain.id, domain]));
  for (const column of fleet?.columns ?? []) {
    const domain = validationDomain(column);
    found.set(domain.id, domain);
  }
  for (const node of nodes) {
    for (const run of node.runs) {
      const domain = validationDomain(run.tool);
      found.set(domain.id, domain);
    }
  }
  const coreIds = new Set(CORE_DOMAINS.map((domain) => domain.id));
  return [
    ...CORE_DOMAINS,
    ...[...found.values()].filter((domain) => !coreIds.has(domain.id)).sort((a, b) => a.label.localeCompare(b.label)),
  ];
}

function jobPoint(job: BringupJob): ValidationPoint {
  const domain = validationDomain(job.base || job.name);
  return {
    id: `bringup:${job.name}`,
    domain: domain.id,
    name: job.name,
    status: validationStatus(job.status || job.metric || "", !job.status && Boolean(job.metric)),
    rawStatus: job.status || (job.metric ? "DONE" : "UNKNOWN"),
    reason: job.reason || "",
    source: "bringup",
    timestamp: "",
    jobId: "",
    metric: job.metric || "",
    metricUnit: job.metric_label || "",
  };
}

function runPoint(run: PostRunEvent): ValidationPoint {
  const domain = validationDomain(run.tool);
  return {
    id: `run:${run.activity_id}:${run.job_id}`,
    domain: domain.id,
    name: run.validation_test || run.tool || "validation",
    status: validationStatus(run.state),
    rawStatus: run.state || "UNKNOWN",
    reason: run.reason || (run.failed_job ? "The job failed without node-specific attribution." : ""),
    source: "post-run",
    timestamp: run.ended_at || run.started_at || "",
    jobId: run.job_id || run.activity_id,
    metric: "",
    metricUnit: "",
    affectsCurrent: run.authoritative !== false && !run.history_only,
    provenance: [run.artifact_path, run.map_method ? `mapped by ${run.map_method}` : ""].filter(Boolean).join(" · "),
  };
}

function slurmPoint(node: PostRunNode): ValidationPoint {
  return {
    id: "slurm:current",
    domain: "slurm",
    name: "Current Slurm state",
    status: validationStatus(node.slurm_state),
    rawStatus: node.slurm_state || "UNKNOWN",
    reason: node.slurm_reason || "",
    source: "slurm",
    timestamp: "",
    jobId: "",
    metric: "",
    metricUnit: "",
  };
}

export function validationPoints(node: PostRunNode, bringupNode?: BringupFleetNode): ValidationPoint[] {
  const points: ValidationPoint[] = [slurmPoint(node)];
  for (const job of bringupNode?.jobs ?? []) points.push(jobPoint(job));
  for (const run of node.runs) points.push(runPoint(run));
  for (const audit of [node.hardware_status, node.network_status]) {
    if (!audit) continue;
    points.push({
      id: `audit:${audit.kind}:${audit.run_id}`,
      domain: "node-health",
      name: `${audit.kind} audit`,
      status: validationStatus(audit.status),
      rawStatus: audit.status,
      reason: audit.reason || audit.warnings?.join("; ") || "",
      source: "deep-audit",
      timestamp: audit.updated_at || audit.started_at || "",
      jobId: audit.scheduler_ref || audit.run_id,
      metric: "",
      metricUnit: "",
    });
  }
  for (const finding of node.findings) {
    points.push({
      id: `finding:${finding.issue_id}`,
      domain: "node-health",
      name: finding.category || "finding",
      status: "attention",
      rawStatus: finding.severity || "FAIL",
      reason: finding.reason || "",
      source: "finding",
      timestamp: finding.created_at || "",
      jobId: finding.scheduler_ref || finding.validation_run_id || "",
      metric: "",
      metricUnit: "",
    });
  }
  // DCGM's production contract requires an explicit r1/r3/r4 view. Absence is
  // never promoted to pass; required checks that have never reported are red.
  const currentDcgm = points.filter((point) => point.domain === "dcgm" && point.affectsCurrent !== false);
  for (const test of ["dcgmi-r1", "dcgmi-r3", "dcgmi-r4"]) {
    if (currentDcgm.some((point) => point.name.toLowerCase() === test)) continue;
    points.push({
      id: `dcgm:missing:${test}`,
      domain: "dcgm",
      name: test,
      status: "not_run",
      rawStatus: "NOT_RUN",
      reason: "No indexed result artifact has been reported for this required check.",
      source: "bringup",
      timestamp: "",
      jobId: "",
      metric: "",
      metricUnit: "",
      affectsCurrent: true,
      provenance: "validated-racks index",
    });
  }
  return points;
}

export function aggregateValidationStatus(points: ValidationPoint[]): ValidationStatus {
  if (!points.length) return "unknown";
  const statuses = points.map((point) => point.status);
  if (statuses.every((status) => status === "not_run" || status === "unknown")) {
    return statuses.some((status) => status === "not_run") ? "not_run" : "unknown";
  }
  if (statuses.some((status) => status === "attention")) return "attention";
  if (statuses.some((status) => status === "running")) return "running";
  if (statuses.some((status) => status === "warning")) return "warning";
  if (statuses.some((status) => status === "not_run" || status === "unknown")) return "warning";
  return "pass";
}

export function nodeValidationState(node: PostRunNode, bringupNode: BringupFleetNode | undefined, mode: string): NodeValidationState {
  const allPoints = validationPoints(node, bringupNode);
  const dcgmTest = mode === "dcgm-r1" ? "dcgmi-r1" : mode === "dcgm-r3" ? "dcgmi-r3" : "";
  const points = mode === "all"
    ? allPoints
    : dcgmTest
      ? allPoints.filter((point) => point.domain === "dcgm" && point.name.toLowerCase() === dcgmTest)
      : allPoints.filter((point) => point.domain === mode);
  const currentPoints = points.filter((point) => point.affectsCurrent !== false);
  const validationOnly = mode === "all" ? currentPoints.filter((point) => point.domain !== "slurm") : currentPoints;
  const status = validationOnly.length && validationOnly.every((point) => point.status === "not_run" || point.status === "unknown")
    ? (validationOnly.some((point) => point.status === "not_run") ? "not_run" : "unknown")
    : aggregateValidationStatus(currentPoints);
  const visiblePoints = currentPoints.slice(0, 15);
  const lines = [
    `${node.node} — ${status.replace("_", " ").toUpperCase()}`,
    ...currentPoints.map((point) => {
      const metric = point.metric ? ` · ${point.metric}${point.metricUnit ? ` ${point.metricUnit}` : ""}` : "";
      const job = point.jobId ? ` · Job ${point.jobId}` : "";
      const reason = point.reason ? ` — ${point.reason}` : "";
      const provenance = point.provenance ? ` · ${point.provenance}` : "";
      return `${point.name}: ${point.rawStatus}${metric}${job}${provenance}${reason}`;
    }),
  ];
  if (!currentPoints.length) lines.push("No evidence has been reported for this selection; it is not treated as healthy.");
  const historyCount = points.length - currentPoints.length;
  if (currentPoints.length > visiblePoints.length) lines.push(`${currentPoints.length - visiblePoints.length} additional current checks are available in node details/export.`);
  if (historyCount) lines.push(`${historyCount} older DCGM attempt${historyCount === 1 ? "" : "s"} retained in node details/export.`);
  return { status, running: currentPoints.some((point) => point.status === "running"), points, visiblePoints, tooltip: lines.join("\n") };
}

export function rackRowKey(rack: string): string {
  const match = rack.match(/(?:^|-)dh(\d+)-([a-z])\d+$/i);
  return match ? `dh${match[1]}-${match[2].toLowerCase()}` : rack.replace(/\d+$/, "").replace(/-$/, "") || "unmatched";
}

/** Keep inventory sentinels such as `appserver` out of the physical SU topology. */
export function isPhysicalValidationRack(rack: string): boolean {
  return /(?:^|-)dh\d+-[a-z]\d+$/i.test(rack.trim());
}

function naturalParts(value: string): Array<string | number> {
  return value.split(/(\d+)/).filter(Boolean).map((part) => /^\d+$/.test(part) ? Number(part) : part.toLowerCase());
}

export function naturalCompare(a: string, b: string): number {
  const aa = naturalParts(a);
  const bb = naturalParts(b);
  for (let i = 0; i < Math.max(aa.length, bb.length); i += 1) {
    if (aa[i] === undefined) return -1;
    if (bb[i] === undefined) return 1;
    if (aa[i] === bb[i]) continue;
    if (typeof aa[i] === "number" && typeof bb[i] === "number") return Number(aa[i]) - Number(bb[i]);
    return String(aa[i]).localeCompare(String(bb[i]));
  }
  return 0;
}

export function groupRacksIntoSus<T extends { rack: string; nodes: unknown[] }>(racks: T[]): SuGroup<T["nodes"][number]>[] {
  const suByRow = new Map<string, number>([
    ...Array.from({ length: 11 }, (_, index) => [`dh02-${String.fromCharCode(97 + index)}`, index + 1] as const),
    ["dh01-a", 12], ["dh01-b", 13], ["dh01-c", 14],
  ]);
  const groups = new Map<string, T[]>();
  for (const rack of racks) {
    if (!isPhysicalValidationRack(rack.rack)) continue;
    const key = rackRowKey(rack.rack);
    groups.set(key, [...(groups.get(key) ?? []), rack]);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => (suByRow.get(a) ?? 999) - (suByRow.get(b) ?? 999) || naturalCompare(a, b))
    .map(([rowKey, rows]) => ({
      id: suByRow.has(rowKey) ? `SU${suByRow.get(rowKey)}` : `Unmapped · ${rowKey}`,
      rowKey,
      racks: rows.sort((a, b) => naturalCompare(a.rack, b.rack)) as Array<{ rack: string; nodes: T["nodes"][number][] }>,
    }));
}

export function completionPrediction(
  states: Array<{ state: NodeValidationState; timestamp: string }>,
  now = new Date(),
): EtaPrediction {
  const total = states.length;
  const completed = states.filter((item) => item.state.status === "pass").length;
  const blocked = states.filter((item) => item.state.status === "attention" || item.state.status === "warning" || item.state.status === "not_run").length;
  const unknown = states.filter((item) => item.state.status === "unknown").length;
  const running = states.filter((item) => item.state.status === "running").length;
  const base = { completed, total, blocked, running };
  const methodology = "Uses real timestamped PASS completions only; requires at least two completions spanning one minute. Failed, warning, NOT_RUN, or missing evidence withholds a pass ETA, and a stale completion stream is reported as stalled.";
  if (!total || completed === total) {
    return { ...base, state: "complete", eta: now.toISOString(), label: total ? "Complete" : "No nodes", confidence: "high", samples: completed, ratePerHour: null, methodology };
  }
  if (blocked > 0) {
    return { ...base, state: "blocked", eta: "", label: `Blocked: ${blocked} failed/warning/not run`, confidence: "none", samples: 0, ratePerHour: null, methodology };
  }
  if (unknown > 0) {
    return { ...base, state: "insufficient", eta: "", label: `ETA unavailable: ${unknown} node${unknown === 1 ? "" : "s"} missing validation evidence`, confidence: "none", samples: 0, ratePerHour: null, methodology };
  }
  const times = states
    .filter((item) => item.state.status === "pass")
    .map((item) => Date.parse(item.timestamp))
    .filter(Number.isFinite)
    .sort((a, b) => a - b);
  if (times.length < 2 || times.at(-1)! - times[0] < 60_000) {
    return { ...base, state: "insufficient", eta: "", label: "ETA unavailable: insufficient transition history", confidence: "none", samples: times.length, ratePerHour: null, methodology };
  }
  const intervals = times.slice(1).map((time, index) => time - times[index]).sort((a, b) => a - b);
  const medianInterval = intervals[Math.floor(intervals.length / 2)] || 60_000;
  const staleAfter = Math.max(10 * 60_000, medianInterval * 2);
  if (now.getTime() - times.at(-1)! > staleAfter) {
    return { ...base, state: "stalled", eta: "", label: "ETA unavailable: completion stream stalled", confidence: "none", samples: times.length, ratePerHour: null, methodology };
  }
  const spanHours = (times.at(-1)! - times[0]) / 3_600_000;
  const ratePerHour = (times.length - 1) / spanHours;
  if (!Number.isFinite(ratePerHour) || ratePerHour <= 0) {
    return { ...base, state: "insufficient", eta: "", label: "ETA unavailable: no observed completion rate", confidence: "none", samples: times.length, ratePerHour: null, methodology };
  }
  const eta = new Date(now.getTime() + ((total - completed) / ratePerHour) * 3_600_000);
  const confidence = times.length >= 10 && spanHours >= 1 ? "high" : times.length >= 5 ? "medium" : "low";
  return {
    ...base,
    state: "estimated",
    eta: eta.toISOString(),
    label: `${eta.toLocaleString()} · ${ratePerHour.toFixed(1)} nodes/hour`,
    confidence,
    samples: times.length,
    ratePerHour,
    methodology,
  };
}

export function latestPassTimestamp(state: NodeValidationState): string {
  return state.points
    .filter((point) => point.status === "pass" && point.timestamp && Number.isFinite(Date.parse(point.timestamp)))
    .map((point) => point.timestamp)
    .sort()
    .at(-1) || "";
}
