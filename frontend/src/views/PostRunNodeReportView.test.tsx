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

function manyRacksReport(count: number): PostRunNodeReportResponse {
  const racks = Array.from({ length: count }, (_, i) => {
    const rack = `rack-${String(i).padStart(2, "0")}`;
    const n: PostRunNode = {
      node: `${rack}-gpu1-su1`,
      rack,
      slurm_state: "idle",
      slurm_reason: "",
      participation_count: 0,
      failed_job_participations: 0,
      failure_count: 0,
      distinct_failure_runs: 0,
      flagged: false,
      trend: "flat",
      last_run_at: "",
      last_failure_at: "",
      findings: [],
      hardware_status: null,
      network_status: null,
      runs: [],
    };
    return { rack, nodes: [n] };
  });
  return {
    ok: true,
    generated_at: "2026-08-13T19:17:27Z",
    history_path: "/app/var/post-run.json",
    summary: { nodes: count, participations: 0, attributed_failures: 0, flagged_nodes: 0, drained_nodes: 0 },
    racks,
    nodes: racks.map((r) => r.nodes[0]),
    refresh: { activities: 0, merge: {}, accounting_error: "", inventory_error: "" },
  };
}

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe("PostRunNodeReportView pagination", () => {
  it("shows at most 10 rack rows and a pager when the floor is larger", async () => {
    service.fetchPostRunNodeReport.mockResolvedValue(manyRacksReport(12));
    render(<PostRunNodeReportView />);

    // First page: racks 00-09 render; 10 and 11 are on page 2.
    expect(await screen.findByText("rack-00")).toBeInTheDocument();
    expect(screen.getByText("rack-09")).toBeInTheDocument();
    expect(screen.queryByText("rack-10")).not.toBeInTheDocument();
    expect(screen.queryByText("rack-11")).not.toBeInTheDocument();

    // The pager reports the filtered total, not the page size.
    expect(screen.getByText(/of 12 racks/)).toBeInTheDocument();
  });

  it("renders no pager when everything fits on one page", async () => {
    service.fetchPostRunNodeReport.mockResolvedValue(manyRacksReport(4));
    render(<PostRunNodeReportView />);
    expect(await screen.findByText("rack-03")).toBeInTheDocument();
    expect(screen.queryByText(/of 4 racks/)).not.toBeInTheDocument();
  });
});

describe("PostRunNodeReportView deep-audit launch", () => {
  it("polls to terminal state and replaces the open dialog with the job and output", async () => {
    service.fetchPostRunNodeReport.mockResolvedValueOnce(report(false)).mockResolvedValue(report(true));
    service.startDeepAudit.mockResolvedValue(runId);
    service.fetchDeepAudit.mockResolvedValue({ state: "completed", run_id: runId });
    service.fetchDeepAuditOutput.mockResolvedValue("ink50-dh01-d01-32: PASS - healthy");
    render(<PostRunNodeReportView />);
    const nodeLabel = await screen.findByText("d01-32");
    fireEvent.click(nodeLabel.closest("button") as HTMLButtonElement);
    fireEvent.click(screen.getByRole("button", { name: /run hardware audit/i }));
    // The launch asks through the themed ConfirmDialog, not window.confirm: accept it there.
    fireEvent.click(await screen.findByRole("button", { name: "Run audit" }));

    await waitFor(() => expect(service.fetchDeepAudit).toHaveBeenCalledWith(runId));
    expect(await screen.findAllByText(/Job 147892/)).not.toHaveLength(0);
    expect(screen.getByText("Validation participation (1)")).toBeInTheDocument();
    // findAll, not getAll: the confirm dialog is still fading out and hides the page until it is gone.
    fireEvent.click((await screen.findAllByRole("button", { name: "Full output" }))[0]);
    expect(await screen.findByText(/d01-32: PASS - healthy/)).toBeInTheDocument();
  });
});


describe("PostRunNodeReportView grouping follows the cluster's mapping", () => {
  function nodeAt(name: string, rack: string): PostRunNode {
    return { ...node(false), node: name, rack };
  }

  it("shows a translated cluster by its mapped racks and Scalable Units, not hostname prefixes", async () => {
    const a = nodeAt("sys1-gso1-p-phy-gpu19-su19", "U20");
    const b = nodeAt("sys1-gso1-p-phy-gpu19-su20", "U21");
    service.fetchPostRunNodeReport.mockResolvedValue({
      ...report(false),
      rack_mapping_mode: "translate",
      racks: [
        { rack: "U20", label: "U20", kind: "rack", scalable_unit: { id: "su19", label: "SU19" }, nodes: [a] },
        { rack: "U21", label: "U21", kind: "rack", scalable_unit: { id: "su20", label: "SU20" }, nodes: [b] },
      ],
      nodes: [a, b],
    });
    render(<PostRunNodeReportView />);
    expect(await screen.findByText("U20")).toBeInTheDocument();
    expect(screen.getByText("SU19")).toBeInTheDocument();
    expect(screen.getByText("U21")).toBeInTheDocument();
    expect(screen.queryByText("sys1-gso1-p-phy-gpu19")).not.toBeInTheDocument();
  });

  it("puts a pool cluster in one pool card and offers no rack picker", async () => {
    const nodes = ["gpu1", "gpu2", "gpu3"].map((n) => nodeAt(`pool-${n}`, ""));
    service.fetchPostRunNodeReport.mockResolvedValue({
      ...report(false),
      rack_mapping_mode: "pool",
      racks: [{ rack: "pool", label: "Node pool", kind: "pool", scalable_unit: null, nodes }],
      nodes,
    });
    render(<PostRunNodeReportView />);
    expect(await screen.findByText("Node pool")).toBeInTheDocument();
    expect(screen.getByText("3 nodes")).toBeInTheDocument();
    expect(screen.getByText(/no rack placement/i)).toBeInTheDocument();
    expect(screen.queryByLabelText("Rack")).not.toBeInTheDocument();
  });

  function unplacedReport(count: number) {
    const placed = nodeAt("sys1-gso1-p-phy-gpu19-su19", "U20");
    const orphans = Array.from({ length: count }, (_, i) => nodeAt(`orphan-${i}`, ""));
    return {
      ...report(false),
      rack_mapping_mode: "translate" as const,
      racks: [
        { rack: "U20", label: "U20", kind: "rack" as const, scalable_unit: null, nodes: [placed] },
        { rack: "unplaced", label: "Unplaced", kind: "unplaced" as const, scalable_unit: null, nodes: orphans },
      ],
      nodes: [placed, ...orphans],
    };
  }

  it("hides unplaced nodes by default and says how many the checkbox would show", async () => {
    service.fetchPostRunNodeReport.mockResolvedValue(unplacedReport(3));
    render(<PostRunNodeReportView />);
    expect(await screen.findByText("U20")).toBeInTheDocument();
    expect(screen.queryByText("Unplaced")).not.toBeInTheDocument();
    expect(screen.getByText(/Show unplaced nodes \(3\)/)).toBeInTheDocument();
  });

  it("shows the unplaced pool when the box is checked, and remembers the choice", async () => {
    service.fetchPostRunNodeReport.mockResolvedValue(unplacedReport(3));
    const first = render(<PostRunNodeReportView />);
    fireEvent.click(await screen.findByRole("checkbox", { name: /show unplaced nodes/i }));
    expect(await screen.findByText("Unplaced")).toBeInTheDocument();
    expect(screen.getByText(/did not place them, so they are listed as a pool/i)).toBeInTheDocument();
    first.unmount();
    render(<PostRunNodeReportView />);
    expect(await screen.findByText("Unplaced")).toBeInTheDocument();
  });

  it("draws a big unplaced pool in steps instead of thousands of tiles at once", async () => {
    localStorage.setItem("nvis.postrun.showUnplaced", "1");
    service.fetchPostRunNodeReport.mockResolvedValue(unplacedReport(300));
    render(<PostRunNodeReportView />);
    expect(await screen.findByText(/Showing 120 of 300/)).toBeInTheDocument();
    expect(screen.getAllByText(/^orphan-/)).toHaveLength(120);
    fireEvent.click(screen.getByRole("button", { name: /show 120 more/i }));
    expect(await screen.findByText(/Showing 240 of 300/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /show all/i }));
    await waitFor(() => expect(screen.getAllByText(/^orphan-/)).toHaveLength(300));
  });

  it("offers no checkbox when every node is placed", async () => {
    service.fetchPostRunNodeReport.mockResolvedValue(report(false));
    render(<PostRunNodeReportView />);
    await screen.findByText("ink50-dh01-d01");
    expect(screen.queryByRole("checkbox", { name: /show unplaced nodes/i })).not.toBeInTheDocument();
  });

  it("warns while placement is still loading", async () => {
    const r = unplacedReport(1);
    service.fetchPostRunNodeReport.mockResolvedValue({ ...r, placement_error: "Node placement is still loading" });
    render(<PostRunNodeReportView />);
    expect(await screen.findByText(/Rack placement is not available yet/i)).toBeInTheDocument();
  });
});
