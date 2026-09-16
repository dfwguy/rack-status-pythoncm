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
import { describe, expect, it } from "vitest";
import type { BringupFleet } from "../types/bringup";
import type { PostRunNode } from "../types/api";
import {
  aggregateValidationStatus,
  availableValidationDomains,
  completionPrediction,
  groupRacksIntoSus,
  nodeValidationState,
  schedulerStatusColor,
  validationStatus,
  validationDomain,
  type NodeValidationState,
  type ValidationPoint,
} from "./validationStatus";

function node(): PostRunNode {
  return {
    node: "ink50-dh01-a01-11", rack: "ink50-dh01-a01", slurm_state: "idle", slurm_reason: "",
    participation_count: 0, failed_job_participations: 0, failure_count: 0, distinct_failure_runs: 0,
    flagged: false, trend: "flat", last_run_at: "", last_failure_at: "", runs: [], findings: [],
    hardware_status: null, network_status: null,
  };
}

const fleet: BringupFleet = {
  home: "/cluster/bringup",
  columns: ["dcgmi-r4", "llm-pretrain", "llm-pretrain-solo", "mnubergemm", "site-special"],
  metric_labels: {},
  nodes: [{
    node: node().node,
    slurm_state: "idle",
    jobs: [
      { name: "dcgmi-r4", base: "dcgmi-r4", status: "DONE", reason: "", metric: null, metric_label: null },
      { name: "llm-pretrain", base: "llm-pretrain", status: "FAIL", reason: "loss diverged", metric: null, metric_label: null },
      { name: "llm-pretrain-solo", base: "llm-pretrain-solo", status: "NOT_RUN", reason: "", metric: null, metric_label: null },
      { name: "mnubergemm", base: "mnubergemm", status: "RUNNING", reason: "job active", metric: null, metric_label: null },
      { name: "site-special", base: "site-special", status: "DONE", reason: "", metric: null, metric_label: null },
    ],
  }],
};

describe("validation domains and All aggregation", () => {
  it("retains dcgmi-r4, llm-pretrain, llm-pretrain-solo, and discovered categories", () => {
    expect(validationDomain("dcgmi-r4").id).toBe("dcgm");
    expect(validationDomain("llm-pretrain").id).toBe("llm-pretrain");
    expect(validationDomain("llm-pretrain-solo").id).toBe("llm-pretrain-solo");
    const ids = availableValidationDomains(fleet, [node()]).map((domain) => domain.id);
    expect(ids).toEqual(expect.arrayContaining(["all", "dcgm", "dcgm-r1", "dcgm-r3", "llm-pretrain", "llm-pretrain-solo", "mnubergemm", "validation:site-special"]));
    const labels = availableValidationDomains(fleet, [node()]).map((domain) => domain.label);
    expect(labels).toEqual(expect.arrayContaining(["DCGM diag -r1", "DCGM diag -r3"]));
  });

  it("combines every real point in All and isolates a selected source domain", () => {
    const all = nodeValidationState(node(), fleet.nodes[0], "all");
    expect(all.points.map((point) => point.name)).toEqual(expect.arrayContaining(["dcgmi-r4", "llm-pretrain", "llm-pretrain-solo", "mnubergemm", "site-special"]));
    expect(all.running).toBe(true);
    const dcgm = nodeValidationState(node(), fleet.nodes[0], "dcgm");
    expect(dcgm.status).toBe("warning");
    expect(dcgm.points.map((point) => point.name)).toEqual(["dcgmi-r4", "dcgmi-r1", "dcgmi-r3"]);
  });

  it("uses only latest canonical DCGM attempts for color while retaining retries and history", () => {
    const value = node();
    value.runs = [
      { node: value.node, rack: value.rack, job_id: "100", activity_id: "old", tool: "dcgmi-r1", validation_test: "dcgmi-r1", scope: "single-node", state: "PASS", started_at: "", ended_at: "", exit_code: "0:0", failed_job: false, attributed_failure: false, category: "", reason: "old", source: "nscale16k_dcgm_index", authoritative: false, artifact_path: "/old" },
      { node: value.node, rack: value.rack, job_id: "101", activity_id: "new", tool: "dcgmi-r1", validation_test: "dcgmi-r1", scope: "single-node", state: "FAIL", started_at: "", ended_at: "", exit_code: "1:0", failed_job: true, attributed_failure: true, category: "gpu", reason: "new failure", source: "nscale16k_dcgm_index", authoritative: true, artifact_path: "/new" },
      { node: value.node, rack: value.rack, job_id: "90", activity_id: "retry", tool: "dcgmi-r1.prev1", validation_test: "dcgmi-r1", scope: "single-node", state: "PASS", started_at: "", ended_at: "", exit_code: "0:0", failed_job: false, attributed_failure: false, category: "", reason: "retry archive", source: "nscale16k_dcgm_index", authoritative: false, history_only: true, artifact_path: "/prev" },
      { node: value.node, rack: value.rack, job_id: "102", activity_id: "r3", tool: "dcgmi-r3", validation_test: "dcgmi-r3", scope: "single-node", state: "INCOMPLETE", started_at: "", ended_at: "", exit_code: "", failed_job: false, attributed_failure: false, category: "", reason: "partial", source: "nscale16k_dcgm_index", authoritative: true, artifact_path: "/r3" },
    ];
    const dcgm = nodeValidationState(value, undefined, "dcgm");
    expect(dcgm.status).toBe("attention");
    expect(dcgm.visiblePoints.map((point) => [point.name, point.status])).toEqual([
      ["dcgmi-r1", "attention"], ["dcgmi-r3", "warning"], ["dcgmi-r4", "not_run"],
    ]);
    expect(dcgm.points).toHaveLength(5);
    expect(dcgm.tooltip).toContain("2 older DCGM attempts");
    expect(dcgm.tooltip).toContain("/new");
  });

  it("exposes independent r1 and r3 health-check views without r4 affecting their status", () => {
    const value = node();
    value.runs = [
      { node: value.node, rack: value.rack, job_id: "201", activity_id: "r1", tool: "dcgmi-r1", validation_test: "dcgmi-r1", scope: "single-node", state: "PASS", started_at: "", ended_at: "", exit_code: "0:0", failed_job: false, attributed_failure: false, category: "", reason: "r1 passed", source: "nscale16k_dcgm_index", authoritative: true, artifact_path: "/health/dcgmi-r1.txt" },
      { node: value.node, rack: value.rack, job_id: "202", activity_id: "r3", tool: "dcgmi-r3", validation_test: "dcgmi-r3", scope: "single-node", state: "FAIL", started_at: "", ended_at: "", exit_code: "1:0", failed_job: true, attributed_failure: true, category: "gpu", reason: "r3 failed", source: "nscale16k_dcgm_index", authoritative: true, artifact_path: "/health/dcgmi-r3.txt" },
    ];

    const r1 = nodeValidationState(value, undefined, "dcgm-r1");
    expect(r1.status).toBe("pass");
    expect(r1.points.map((point) => point.name)).toEqual(["dcgmi-r1"]);
    expect(r1.tooltip).toContain("/health/dcgmi-r1.txt");

    const r3 = nodeValidationState(value, undefined, "dcgm-r3");
    expect(r3.status).toBe("attention");
    expect(r3.points.map((point) => point.name)).toEqual(["dcgmi-r3"]);
    expect(r3.tooltip).toContain("r3 failed");
  });

  it("marks a missing dedicated r1 or r3 health check as NOT_RUN", () => {
    const r1 = nodeValidationState(node(), undefined, "dcgm-r1");
    const r3 = nodeValidationState(node(), undefined, "dcgm-r3");
    expect([r1.status, r1.points[0].rawStatus]).toEqual(["not_run", "NOT_RUN"]);
    expect([r3.status, r3.points[0].rawStatus]).toEqual(["not_run", "NOT_RUN"]);
  });

  it("uses the OVA single-node health-check event in the Node Health view", () => {
    const value = node();
    value.runs = [{
      node: value.node, rack: value.rack, job_id: "health-1788396873", activity_id: "node-health:1788396873",
      tool: "node-health-check", validation_test: "node-health", scope: "single-node", state: "FAIL",
      started_at: "2026-09-03T01:34:33Z", ended_at: "2026-09-03T01:34:33Z", exit_code: "",
      failed_job: true, attributed_failure: true, category: "hardware",
      reason: "Failed checks: ib-link, imex-channels", source: "nscale16k_healthcheck_index",
      authoritative: true, artifact_path: "/cluster/OVA/healthchecks/logs/node/.last-check",
    }];

    const health = nodeValidationState(value, undefined, "node-health");

    expect(health.status).toBe("attention");
    expect(health.points.map((point) => point.name)).toEqual(["node-health"]);
    expect(health.tooltip).toContain("Failed checks: ib-link, imex-channels");
    expect(health.tooltip).toContain(".last-check");
  });

  it("groups the physical dh rows into ordered SUs", () => {
    const groups = groupRacksIntoSus([
      { rack: "appserver", nodes: [{}] },
      { rack: "ink50-dh02-k24", nodes: [] },
      { rack: "ink50-dh01-a01", nodes: [] },
      { rack: "ink50-dh01-a02", nodes: [] },
    ]);
    expect(groups.map((group) => [group.id, group.rowKey])).toEqual([["SU11", "dh02-k"], ["SU12", "dh01-a"]]);
    expect(groups[1].racks).toHaveLength(2);
    expect(groups.flatMap((group) => group.racks).some((rack) => rack.rack === "appserver")).toBe(false);
  });

  it("uses the production SU row contract rather than lexical row order", () => {
    const expected = [
      ...Array.from({ length: 11 }, (_, index) => [`SU${index + 1}`, `dh02-${String.fromCharCode(97 + index)}`]),
      ["SU12", "dh01-a"], ["SU13", "dh01-b"], ["SU14", "dh01-c"],
    ];
    const groups = groupRacksIntoSus(expected.slice().reverse().map(([, row]) => ({
      rack: `ink50-${row}01`, nodes: [],
    })));
    expect(groups.map((group) => [group.id, group.rowKey])).toEqual(expected);
  });
});

describe("source status contract", () => {
  it.each([
    ["UP", "pass"], [" up*  ", "pass"], ["IDLE", "pass"], ["DONE", "pass"],
    ["SUCCESS", "pass"], ["COMPLETED", "pass"], ["running", "running"],
    ["ALLOC*", "running"], ["allocated", "running"], ["COMPLETING", "running"],
    ["configuring", "running"], ["PENDING", "warning"], ["inconclusive", "warning"],
    ["degraded", "warning"], ["STALE", "warning"], ["INCOMPLETE", "warning"],
    ["retry", "warning"], ["FAILED", "attention"], [" down* ", "attention"],
    ["DRAINED", "attention"], ["NODE_FAIL", "attention"], ["TIMEOUT", "attention"],
    ["BLOCKED", "attention"], ["CANCELLED", "attention"], ["CANCELED", "attention"],
    ["CANCELLED BY 1003", "attention"], ["NOT RUN", "not_run"],
    ["never checked", "not_run"], ["UNKNOWN", "unknown"], ["unreported", "unknown"],
    ["", "unknown"], ["MIXED", "unknown"], ["RESERVED", "unknown"],
    ["PLANNED", "unknown"], ["MAINT", "unknown"], ["POWER_DOWN", "unknown"],
  ])("maps raw %j to %s", (raw, expected) => {
    expect(validationStatus(raw)).toBe(expected);
  });

  it("accepts a numeric/scientific result only for an explicit metric-only contract", () => {
    expect(validationStatus("2.7e-6")).toBe("unknown");
    expect(validationStatus("2.7e-6", true)).toBe("pass");
    expect(validationStatus("-1.0", true)).toBe("pass");
    expect(validationStatus("FAILED", true)).toBe("attention");
  });

  it.each([
    ["up", "green"], ["idle", "green"], ["down", "red"], ["drain", "red"],
    ["alloc", "yellow"], ["pending", "yellow"], ["unknown", "gray"], ["provider-new-state", "gray"],
  ])("uses the shared scheduler tag color for %s", (raw, expected) => {
    expect(schedulerStatusColor(raw)).toBe(expected);
  });

  it("applies hard-failure, active, warning, and missing precedence without treating missing evidence as healthy", () => {
    const point = (status: ValidationPoint["status"]): ValidationPoint => ({
      id: status, domain: "test", name: status, status, rawStatus: status, reason: "", source: "post-run",
      timestamp: "", jobId: "", metric: "", metricUnit: "",
    });
    expect(aggregateValidationStatus([point("pass"), point("attention"), point("running")])).toBe("attention");
    expect(aggregateValidationStatus([point("pass"), point("running"), point("warning")])).toBe("running");
    expect(aggregateValidationStatus([point("pass"), point("warning")])).toBe("warning");
    expect(aggregateValidationStatus([point("pass"), point("unknown")])).toBe("warning");
    expect(aggregateValidationStatus([point("unknown")])).toBe("unknown");
    expect(aggregateValidationStatus([point("not_run"), point("unknown")])).toBe("not_run");
  });

  it.each([
    ["up", "pass", false], ["down", "attention", false], ["alloc", "running", true], ["unknown", "unknown", false],
  ])("integrates Slurm %s as %s with running=%s", (raw, expected, running) => {
    const value = node();
    value.slurm_state = raw;
    const result = nodeValidationState(value, undefined, "slurm");
    expect(result.status).toBe(expected);
    expect(result.running).toBe(running);
    expect(result.points[0].rawStatus).toBe(raw);
  });
});

function state(status: NodeValidationState["status"]): NodeValidationState {
  return { status, running: status === "running", points: [], visiblePoints: [], tooltip: "" };
}

describe("completion predictions", () => {
  const now = new Date("2026-09-02T12:00:00Z");

  it("estimates only from real timestamped pass transitions", () => {
    const result = completionPrediction([
      { state: state("pass"), timestamp: "2026-09-02T11:20:00Z" },
      { state: state("pass"), timestamp: "2026-09-02T11:40:00Z" },
      { state: state("pass"), timestamp: "2026-09-02T11:55:00Z" },
      { state: state("running"), timestamp: "" },
    ], now);
    expect(result.state).toBe("estimated");
    expect(result.ratePerHour).toBeGreaterThan(0);
    expect(result.eta).not.toBe("");
  });

  it("withholds ETA for blocked, stalled, and insufficient data", () => {
    expect(completionPrediction([{ state: state("attention"), timestamp: "" }], now).state).toBe("blocked");
    expect(completionPrediction([{ state: state("warning"), timestamp: "" }], now).state).toBe("blocked");
    expect(completionPrediction([
      { state: state("pass"), timestamp: "2026-09-02T08:00:00Z" },
      { state: state("pass"), timestamp: "2026-09-02T08:10:00Z" },
      { state: state("running"), timestamp: "" },
    ], now).state).toBe("stalled");
    const missing = completionPrediction([
      { state: state("pass"), timestamp: "2026-09-02T11:55:00Z" },
      { state: state("unknown"), timestamp: "" },
    ], now);
    expect(missing.state).toBe("insufficient");
    expect(missing.label).toContain("missing validation evidence");
  });
});
