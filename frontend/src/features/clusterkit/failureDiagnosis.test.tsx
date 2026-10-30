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
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { failureDiagnosis } from "./failureDiagnosis";
import { RunDetails } from "./RunDetails";
import { completed } from "./testing/fixtures";

const service = vi.hoisted(() => ({ getArtifacts: vi.fn().mockResolvedValue([]), downloadArtifact: vi.fn() }));
vi.mock("./service", () => service);
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const hosts = Array.from({ length: 8 }, (_, i) => `sys1-gso1-p-phy-gpu${i + 37}-su37`);
const props = { activityId: completed.activity_id, error: "", submitting: false, refresh: vi.fn() };
function failed(reporter = hosts[7]) {
  const run = structuredClone(completed);
  run.phase = "error"; run.work_phase = "failed"; run.scheduler_state = "FAILED";
  run.error = "Slurm job ended in FAILED"; run.activity.nodes = hosts;
  run.summary!.nodes = hosts; run.summary!.functional_status = "FAIL";
  run.summary!.preflight!.nodes!.forEach((node, index) => { node.host = hosts[index]; });
  run.summary!.stages = {
    baseline: { exit_status: 143, status: "FAIL", failure: {
      category: "UCX_COMMUNICATION_TIMEOUT", message: "UCX communication timed out",
      affected_nodes: [reporter], evidence: [{ artifact: "baseline.log", line: 9,
        excerpt: `[${reporter}:1573721:0:1573721] ud_ep.c:296 Fatal: UD endpoint to <no debug data>: unhandled timeout error` }],
    }, metrics: { bandwidth: { units: "MB/s", observed_pair_count: 0, missing_pairs: [[hosts[0], hosts[1]]], failed_pairs: [], pairs: [] } } },
    "single-path": { exit_status: null, status: "SKIPPED", reason: "Not run because baseline failed" },
  };
  return run;
}
function explanation() { return screen.getByRole("region", { name: "Failure explanation" }); }

describe("per-attempt failure explanation", () => {
  it("identifies the actual reporting node without diagnosing defective hardware", () => {
    render(<RunDetails {...props} run={failed()} />);
    const panel = within(explanation());
    expect(panel.getByText("Network communication timed out")).toBeVisible();
    expect(panel.getByText(hosts[7])).toBeVisible();
    expect(panel.getByText(/Reported by/)).toBeVisible();
    expect(panel.getByText(/peer and fault location are not established/)).toBeVisible();
    expect(panel.getByText(/8\/8 nodes passed local checks/)).toBeVisible();
    expect(panel.getByText(/bandwidth 0\/28 pairs/)).toBeVisible();
    expect(panel.getByText(/not confirmed failed links/)).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/defective GPU/i)).toBeNull();
    expect(screen.getByText("UNVERIFIED")).toBeVisible();
  });
  it("uses this run's reporter and does not remember the suspected node from another run", () => {
    const { rerender } = render(<RunDetails {...props} run={failed()} />);
    rerender(<RunDetails {...props} run={failed(hosts[1])} />);
    expect(within(explanation()).getByText(hosts[1])).toBeVisible();
    expect(within(explanation()).queryByText(hosts[7])).toBeNull();
  });
  it("keeps raw evidence collapsed until requested, then shows artifact and line", async () => {
    render(<RunDetails {...props} run={failed()} />);
    const trigger = within(explanation()).getByText("Evidence and next step").closest("summary")!;
    expect(trigger.closest("details")).not.toHaveAttribute("open");
    expect(within(explanation()).getByText(/ud_ep.c:296/)).not.toBeVisible();
    fireEvent.click(trigger);
    await waitFor(() => expect(trigger.closest("details")).toHaveAttribute("open"));
    expect(within(explanation()).getByText("baseline.log · line 9")).toBeVisible();
    expect(within(explanation()).getByText(/ud_ep.c:296/)).toBeVisible();
    expect(service.getArtifacts).not.toHaveBeenCalled();
  });
  it("highlights the reporting row separately from its local preflight PASS", async () => {
    render(<RunDetails {...props} run={failed()} />);
    fireEvent.click(within(explanation()).getByRole("button", { name: "Show node diagnostics" }));
    expect(screen.getByRole("columnheader", { name: "Local preflight" })).toBeVisible();
    const finding = screen.getByText("Communication timeout reported");
    const row = finding.closest("tr")!;
    expect(row).toHaveAttribute("data-run-finding", "true");
    expect(within(row).getByText(hosts[7])).toBeVisible();
    expect(within(row).getByText("PASS")).toBeVisible();
    expect(screen.getAllByText("Communication timeout reported")).toHaveLength(1);
    await waitFor(() => expect(screen.getByRole("region", { name: "Node diagnostics" })).toHaveFocus());
  });
  it("opens the existing artifact browser only on user action", async () => {
    render(<RunDetails {...props} run={failed()} />);
    expect(service.getArtifacts).not.toHaveBeenCalled();
    fireEvent.click(within(explanation()).getByRole("button", { name: "View artifacts" }));
    await waitFor(() => expect(service.getArtifacts).toHaveBeenCalledTimes(1));
    expect(service.getArtifacts).toHaveBeenCalledWith(props.activityId, expect.any(AbortSignal));
    expect(service.downloadArtifact).not.toHaveBeenCalled();
  });
  it("does not invent a reporter for older summaries", () => {
    const run = failed(); delete run.summary!.stages!.baseline.failure;
    render(<RunDetails {...props} run={run} />);
    expect(within(explanation()).getByText("ClusterKit stage failed")).toBeVisible();
    expect(within(explanation()).getByText(/No node identified/)).toBeVisible();
    expect(within(explanation()).queryByText(hosts[7])).toBeNull();
  });
  it("does not use nodes outside the exact verified allocation", () => {
    const run = failed("another-cluster-node");
    expect(failureDiagnosis(run)!.nodes).toEqual([]);
    run.summary!.nodes = hosts.slice(0, 7);
    run.summary!.stages!.baseline.failure!.affected_nodes = [hosts[7]];
    expect(failureDiagnosis(run)!.nodes).toEqual([]);
  });
  it("does not diagnose a successful run from stale error metadata", () => {
    const run = failed(); run.phase = "done"; run.scheduler_state = "COMPLETED";
    run.summary!.functional_status = "PASS";
    render(<RunDetails {...props} run={run} />);
    expect(screen.queryByRole("region", { name: "Failure explanation" })).toBeNull();
  });
  it("preserves pending and uncertain-submission guidance without diagnosing a node", () => {
    const run = structuredClone(completed); run.phase = "running"; run.scheduler_state = "RUNNING";
    expect(failureDiagnosis(run)).toBeNull();
    const unknown = failed(); unknown.work_phase = "submission_unknown";
    render(<RunDetails {...props} run={unknown} />);
    expect(screen.queryByRole("region", { name: "Failure explanation" })).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("No submission will be retried");
  });
  it("never trusts node or preflight findings when summary verification failed", () => {
    const run = failed(); run.summary_error = "Summary hash mismatch";
    render(<RunDetails {...props} run={run} />);
    const panel = within(explanation());
    expect(panel.getByText("Run evidence could not be verified")).toBeVisible();
    expect(panel.queryByText(hosts[7])).toBeNull();
    expect(panel.queryByText(/8\/8 nodes passed/)).toBeNull();
    expect(screen.getByText("INVALID EVIDENCE")).toBeVisible();
  });
  it("shows a failed local check and its error rather than claiming a network timeout", () => {
    const run = failed(); run.summary!.preflight!.status = "FAIL";
    run.summary!.preflight!.nodes![3].status = "FAIL";
    run.summary!.preflight!.nodes![3].error = "Selected HCA port is not ACTIVE";
    render(<RunDetails {...props} run={run} />);
    expect(within(explanation()).getByText("Node preflight failed")).toBeVisible();
    expect(within(explanation()).getByText(hosts[3])).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Show node diagnostics" }));
    expect(screen.getByText("Selected HCA port is not ACTIVE")).toBeVisible();
    expect(screen.getByText("Local check failed")).toBeVisible();
  });
  it("retains the baseline failure when comparison is skipped or independently fails", () => {
    const run = failed();
    run.summary!.stages!["single-path"] = { exit_status: 1, status: "FAIL", failure: {
      category: "PROCESS_FAILED", message: "Other failure", affected_nodes: [hosts[0]],
    } };
    expect(failureDiagnosis(run)!.nodes).toEqual([hosts[7]]);
    expect(failureDiagnosis(run)!.stage).toBe("Baseline");
  });
  it("describes a single-path-only failure and deduplicates reporters", () => {
    const run = failed(); const failure = run.summary!.stages!.baseline;
    run.summary!.stages = { baseline: { exit_status: 0, status: "PASS" }, "single-path": failure };
    failure.failure!.affected_nodes!.push(hosts[7]);
    expect(failureDiagnosis(run)!.stage).toBe("Single-path");
    expect(failureDiagnosis(run)!.nodes).toEqual([hosts[7]]);
  });
  it("handles scheduler failures without a summary honestly", () => {
    const run = failed(); run.summary = null;
    const result = failureDiagnosis(run)!;
    expect(result.nodes).toEqual([]);
    expect(result.title).toBe("Run did not complete successfully");
    expect(result.preflight).toBe("No local preflight evidence");
  });
  it("treats excerpts as inert text, limits them, and never uses artifact paths as URLs", async () => {
    const run = failed();
    run.summary!.stages!.baseline.failure!.evidence = [{ artifact: "../../private/file",
      line: -2, excerpt: '<img src=x onerror="alert(1)">' + "x".repeat(2000) }];
    render(<RunDetails {...props} run={run} />);
    const trigger = within(explanation()).getByText("Evidence and next step").closest("summary")!;
    fireEvent.click(trigger);
    await waitFor(() => expect(trigger.closest("details")).toHaveAttribute("open"));
    expect(explanation().querySelector("img")).toBeNull();
    expect(explanation().querySelector("a")).toBeNull();
    expect(explanation().querySelector("pre")!.textContent!.length).toBe(1200);
    expect(within(explanation()).queryByText(/line -2/)).toBeNull();
    expect(service.downloadArtifact).not.toHaveBeenCalled();
  });
});
