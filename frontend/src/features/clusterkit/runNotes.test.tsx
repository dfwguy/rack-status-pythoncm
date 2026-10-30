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
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { RunDetails } from "./RunDetails";
import { completed } from "./testing/fixtures";

vi.mock("./service", () => ({ getArtifacts: vi.fn(), downloadArtifact: vi.fn() }));
afterEach(cleanup);
const props = { activityId: completed.activity_id, error: "", submitting: false, refresh: vi.fn() };
const warning = "Raw bandwidth and latency are measurements, not performance acceptance.";
function notesTrigger(count: number) {
  const trigger = screen.getByText("Run notes (" + count + ")").closest("summary");
  if (!trigger) throw new Error("Missing run-notes disclosure");
  return trigger;
}

it("keeps routine notes collapsed without alerts, deduplicating repeated messages", async () => {
  const run = structuredClone(completed);
  run.summary!.warnings = [warning, " " + warning + " ", "Vendor PPN is metadata."];
  render(<RunDetails {...props} run={run} />);
  const trigger = notesTrigger(2);
  expect(trigger.closest("details")).not.toHaveAttribute("open");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByText("PASS")).toBeInTheDocument();
  expect(screen.getByText("UNVERIFIED")).toBeInTheDocument();
  fireEvent.click(trigger);
  await waitFor(() => expect(trigger.closest("details")).toHaveAttribute("open"));
  expect(trigger.closest("details")).toHaveAttribute("open");
  expect(screen.getByText(warning)).toBeVisible();
  expect(screen.getAllByText(warning)).toHaveLength(1);
});

it("keeps failures apparent while putting complete error details in the disclosure", async () => {
  const run = structuredClone(completed);
  run.phase = "error";
  run.error = "Selected network device is down";
  run.summary!.functional_status = "FAIL";
  run.summary!.errors = [run.error, "Global RoCE GID was not found"];
  render(<RunDetails {...props} run={run} />);
  expect(screen.getByText("FAIL")).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Failure explanation" })).toBeVisible();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  const trigger = notesTrigger(2);
  expect(trigger.closest("details")).not.toHaveAttribute("open");
  fireEvent.click(trigger);
  await waitFor(() => expect(trigger.closest("details")).toHaveAttribute("open"));
  expect(screen.getByText(run.error)).toBeVisible();
  expect(screen.getByText("Global RoCE GID was not found")).toBeVisible();
});

it("retains uncertain-submission guidance without a warning banner", async () => {
  const run = { ...completed, phase: "running", work_phase: "submission_unknown", scheduler_state: "SUBMISSION_UNKNOWN" };
  render(<RunDetails {...props} run={run} error="Lost submission response; recovering by attempt ID" />);
  expect(screen.getByRole("status")).toHaveTextContent("No submission will be retried");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  fireEvent.click(notesTrigger(1));
  await waitFor(() => expect(notesTrigger(1).closest("details")).toHaveAttribute("open"));
  expect(screen.getByText("Lost submission response; recovering by attempt ID")).toBeVisible();
});

it("does not open notes on polling updates or close a disclosure the user has opened", async () => {
  const run = structuredClone(completed);
  run.summary!.warnings = [warning];
  const { rerender } = render(<RunDetails {...props} run={run} />);
  const trigger = notesTrigger(1);
  rerender(<RunDetails {...props} run={structuredClone(run)} />);
  expect(trigger.closest("details")).not.toHaveAttribute("open");
  fireEvent.click(trigger);
  await waitFor(() => expect(trigger.closest("details")).toHaveAttribute("open"));
  rerender(<RunDetails {...props} run={structuredClone(run)} />);
  expect(trigger.closest("details")).toHaveAttribute("open");
  expect(screen.getByText(warning)).toBeVisible();
});

it("shows a skipped comparison once for historical failed summaries without hiding the baseline failure", async () => {
  const run = structuredClone(completed);
  run.phase = "error"; run.error = "Slurm job ended in FAILED";
  run.summary!.functional_status = "FAIL";
  run.summary!.clusterkit_exit_status = { baseline: 143 };
  run.summary!.stages = {
    baseline: { exit_status: 143, metrics: {} },
    "single-path": { exit_status: null, metrics: {} },
  };
  run.summary!.errors = ["baseline: process did not complete successfully",
    "single-path: process did not complete successfully",
    ...Array.from({length:18}, (_, rank) => "single-path rank " + rank + ": [Errno 2] No such file or directory: 'rank-placement-single-path-" + rank + ".json'"),
    "single-path: Missing bandwidth JSON"];
  render(<RunDetails {...props} run={run} />);
  expect(screen.getByText("FAIL")).toBeInTheDocument();
  expect(screen.getByText("Not run")).toBeInTheDocument();
  expect(screen.getByText("Not run because baseline failed")).toBeInTheDocument();
  fireEvent.click(notesTrigger(3));
  await waitFor(() => expect(notesTrigger(3).closest("details")).toHaveAttribute("open"));
  expect(screen.getByText("Slurm job ended in FAILED")).toBeVisible();
  expect(screen.getByText("baseline: process did not complete successfully")).toBeVisible();
  expect(screen.queryByText(/single-path rank /)).toBeNull();
});

it("retains missing artifacts when the comparison was attempted", async () => {
  const run = structuredClone(completed);
  run.phase = "error"; run.summary!.functional_status = "FAIL";
  run.summary!.clusterkit_exit_status = {baseline: 0, "single-path": 1};
  run.summary!.stages = {"single-path": {exit_status: 1, metrics: {}}};
  run.summary!.errors = ["single-path rank 0: No such file or directory"];
  render(<RunDetails {...props} run={run} />);
  expect(screen.queryByText("Not run")).toBeNull();
  fireEvent.click(notesTrigger(1));
  await waitFor(() => expect(notesTrigger(1).closest("details")).toHaveAttribute("open"));
  expect(screen.getByText(run.summary!.errors[0])).toBeVisible();
});
