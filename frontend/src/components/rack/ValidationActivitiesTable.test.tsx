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
import { ValidationActivitiesTable } from "./ValidationActivitiesTable";
import type { ValidationActivity } from "../../types/api";

const sampleActivity: ValidationActivity = {
  activity_id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
  job_id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
  tool: "llmb-nccl",
  kind: "llmb",
  stage: "stage1",
  scope: "rack",
  rack: "p5-r01",
  racks: ["p5-r01"],
  log_dir: "/logs",
  operator: "alice",
  command_display: "run_nccl_mpi.sh --dry-run",
  scheduler: "slurm",
  dry_run: true,
  phase: "done",
  work_phase: "running",
  returncode: null,
  error: null,
  created_at: "2026-07-31T12:00:00.000Z",
  updated_at: "2026-07-31T12:00:00.000Z",
  finished_at: "2026-07-31T12:00:05.000Z",
  stdout_tail: "SUBMITTED: jobid=12345",
  stderr_tail: "",
  source: "ui",
  tasks: [
    {
      task_id: "t1",
      ordinal: 1,
      label: "nccl",
      scheduler_ref: "12345",
      scheduler_state: "RUNNING",
      phase: "running",
      work_phase: "running",
      evidence: "squeue",
      stdout_path: "/logs/out/12345.out",
      training_log_path: null,
      results_log_path: null,
      updated_at: "2026-07-31T12:00:00.000Z",
    },
  ],
};

function mockFetchActivities(activities: ValidationActivity[]) {
  return vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ ok: true, activities }),
  } as Response);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ValidationActivitiesTable", () => {
  it("renders activities with task scheduler refs", async () => {
    vi.stubGlobal("fetch", mockFetchActivities([sampleActivity]));
    render(<ValidationActivitiesTable logDir="/logs" rack="p5-r01" pollMs={0} />);
    await waitFor(() => expect(screen.getByText("p5-r01")).toBeInTheDocument());
    expect(screen.getByText(/NCCL \(LLMB\)/)).toBeInTheDocument();
    expect(screen.getByText(/nccl 12345 \[running\]/)).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.getByText("launch done")).toBeInTheDocument();
    expect(screen.getByText("alice")).toBeInTheDocument();
  });

  it("shows empty state when no activities", async () => {
    vi.stubGlobal("fetch", mockFetchActivities([]));
    render(<ValidationActivitiesTable logDir="/logs" pollMs={0} />);
    await waitFor(() =>
      expect(screen.getByText(/No validation activities yet/)).toBeInTheDocument(),
    );
  });

  it("dismisses an activity via DELETE", async () => {
    const listFetch = mockFetchActivities([sampleActivity]);
    const deleteFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true }),
    } as Response);
    vi.stubGlobal(
      "fetch",
      vi.fn((_input: RequestInfo, init?: RequestInit) => {
        if (init?.method === "DELETE") return deleteFetch();
        return listFetch();
      }),
    );

    render(<ValidationActivitiesTable logDir="/logs" pollMs={0} />);
    await waitFor(() => expect(screen.getByText("p5-r01")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(deleteFetch).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByText(/No validation activities yet/)).toBeInTheDocument(),
    );
  });

  it("refetches on demand, so polling is never the only way to see a new launch", async () => {
    const listFetch = mockFetchActivities([sampleActivity]);
    vi.stubGlobal("fetch", listFetch);
    render(<ValidationActivitiesTable logDir="/logs" pollMs={0} />);
    await waitFor(() => expect(screen.getByText("p5-r01")).toBeInTheDocument());
    const before = listFetch.mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: "Refresh validation activities" }));
    await waitFor(() => expect(listFetch.mock.calls.length).toBeGreaterThan(before));
  });

  it("says why the panel is empty instead of just showing zero", async () => {
    /**
     * The failure this guards: with the backend half-deployed the endpoint returned the SPA's
     * HTML, the fetch threw, and the panel read "0 tracked" with no hint that anything was
     * wrong -- which reads as "the launch was not captured".
     */
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("no such API endpoint")));
    render(<ValidationActivitiesTable logDir="/logs" pollMs={0} />);
    expect(
      await screen.findByText(/Could not load validation activities: no such API endpoint/),
    ).toBeInTheDocument();
  });
});
