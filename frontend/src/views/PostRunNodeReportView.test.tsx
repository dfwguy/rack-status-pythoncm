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
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PostRunNode, PostRunNodeReportResponse } from "../types/api";
import { PostRunNodeReportView } from "./PostRunNodeReportView";

const service = vi.hoisted(() => ({
  fetchDeepAudit: vi.fn(),
  fetchDeepAuditOutput: vi.fn(),
  fetchPostRunNodeReport: vi.fn(),
  markPostRunNode: vi.fn(),
  startDeepAudit: vi.fn(),
}));

vi.mock("../services/nodeIssuesService", () => service);
vi.mock("../services/bringupService", () => ({
  getBringupFleet: vi.fn(async (nodes: string[]) => ({
    home: "/cluster/bringup",
    columns: ["dcgmi-r4", "llm-pretrain", "llm-pretrain-solo"],
    metric_labels: {},
    nodes: nodes.map((name) => ({
      node: name,
      slurm_state: "idle",
      jobs: [
        { name: "dcgmi-r4", base: "dcgmi-r4", status: "DONE", reason: "", metric: null, metric_label: null },
        { name: "llm-pretrain", base: "llm-pretrain", status: "DONE", reason: "", metric: null, metric_label: null },
        { name: "llm-pretrain-solo", base: "llm-pretrain-solo", status: "DONE", reason: "", metric: null, metric_label: null },
      ],
    })),
  })),
}));

const runId = "ae74eb2d-d38e-4a38-b6e3-0f1dfb474e1e";

function node(withAudit: boolean): PostRunNode {
  const audit = withAudit ? {
    node: "ink50-dh01-d01-32",
    rack: "ink50-dh01-d01",
    kind: "hardware",
    run_id: runId,
    status: "pass" as const,
    scheduler_ref: "147892",
    started_at: "2026-08-13T19:17:24Z",
    updated_at: "2026-08-13T19:17:27Z",
    drained: false,
  } : null;
  return {
    node: "ink50-dh01-d01-32",
    rack: "ink50-dh01-d01",
    slurm_state: "idle",
    slurm_reason: "",
    participation_count: withAudit ? 1 : 0,
    failed_job_participations: 0,
    failure_count: 0,
    distinct_failure_runs: 0,
    flagged: false,
    trend: "flat",
    last_run_at: withAudit ? "2026-08-13T19:17:24Z" : "",
    last_failure_at: "",
    findings: [],
    hardware_status: audit,
    network_status: null,
    runs: withAudit ? [{
      node: "ink50-dh01-d01-32",
      rack: "ink50-dh01-d01",
      job_id: "147892",
      activity_id: runId,
      tool: "hardware-deep-audit",
      scope: "workflow",
      state: "COMPLETED",
      started_at: "2026-08-13T19:17:24Z",
      ended_at: "2026-08-13T19:17:27Z",
      exit_code: "0:0",
      failed_job: false,
      attributed_failure: false,
      category: "",
      reason: "",
      source: "deep-audit-workflow",
      output_run_id: runId,
      output_step_id: "audit",
    }] : [],
  };
}

function report(withAudit: boolean): PostRunNodeReportResponse {
  const item = node(withAudit);
  return {
    ok: true,
    generated_at: "2026-08-13T19:17:27Z",
    history_path: "/app/var/post-run.json",
    summary: { nodes: 1, participations: withAudit ? 1 : 0, attributed_failures: 0, flagged_nodes: 0, drained_nodes: 0 },
    racks: [{ rack: item.rack, nodes: [item] }],
    nodes: [item],
    refresh: { activities: 0, merge: {}, accounting_error: "", inventory_error: "" },
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe("PostRunNodeReportView deep-audit launch", () => {
  it("polls to terminal state and replaces the open dialog with the job and output", async () => {
    service.fetchPostRunNodeReport.mockResolvedValueOnce(report(false)).mockResolvedValue(report(true));
    service.startDeepAudit.mockResolvedValue(runId);
    service.fetchDeepAudit.mockResolvedValue({ state: "completed", run_id: runId });
    service.fetchDeepAuditOutput.mockResolvedValue("ink50-dh01-d01-32: PASS - healthy");
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<PostRunNodeReportView />);
    expect(await screen.findByText("Validation Status")).toBeInTheDocument();
    const nodeCell = await screen.findByRole("button", { name: /Rack ink50-dh01-d01/i });
    expect(screen.queryByText("d01-32")).not.toBeInTheDocument();
    fireEvent.click(nodeCell);
    fireEvent.click(screen.getByRole("button", { name: /run hardware audit/i }));

    await waitFor(() => expect(service.fetchDeepAudit).toHaveBeenCalledWith(runId));
    expect(await screen.findAllByText(/Job 147892/)).not.toHaveLength(0);
    expect(screen.getByText("Validation participation (1)")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "Full output" })[0]);
    expect(await screen.findByText(/d01-32: PASS - healthy/)).toBeInTheDocument();
  });
});
