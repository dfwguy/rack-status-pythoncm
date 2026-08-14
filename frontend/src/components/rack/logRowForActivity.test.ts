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
import { logRowForActivity, type ValidationActivitiesLogRow } from "./logRowForActivity";
import type { ValidationActivity } from "../../types/api";

/**
 * Which parser log belongs to a validation activity.
 *
 * The bug this guards: rack-name matching cannot tell a run apart from an OLDER run whose rack set
 * is a superset. A 4-rack NEMO activity on h18-h21 scored 4 against its own log AND against a
 * stale 5-rack h18-h22 NCCL log; the tie fell to listing order, so "Charts" on job 131920 opened
 * job 127207's results.
 */

function activity(over: Partial<ValidationActivity> = {}): ValidationActivity {
  return {
    activity_id: "act-1",
    job_id: "act-1", // UUID for dashboard launches — must NOT be treated as a Slurm id
    tool: "nemo-xrack",
    kind: "nemo",
    stage: "stage2",
    scope: "multirack",
    rack: "",
    racks: ["ink50-dh01-h18", "ink50-dh01-h19", "ink50-dh01-h20", "ink50-dh01-h21"],
    log_dir: "/logs",
    operator: "rkoike",
    command_display: "run-nemo-xrack.sh ...",
    scheduler: "slurm",
    dry_run: false,
    phase: "done",
    work_phase: "done",
    returncode: 0,
    error: null,
    created_at: "2026-08-07T19:34:00.000Z",
    updated_at: "2026-08-07T19:49:00.000Z",
    finished_at: "2026-08-07T19:49:04.000Z",
    stdout_tail: "",
    stderr_tail: "",
    source: "ui",
    tasks: [],
    ...over,
  } as ValidationActivity;
}

const ownLog: ValidationActivitiesLogRow = {
  log_file: "root-NEMO-ink50-dh01-h18+ink50-dh01-h19+ink50-dh01-h20+ink50-dh01-h21-72N-131920.txt",
  display_rack: "ink50-dh01-h18+ink50-dh01-h19+ink50-dh01-h20+ink50-dh01-h21",
  last_job: "131920",
};

// Older, 5-rack run. A superset of ownLog's racks — the thing that used to win the tie.
const supersetLog: ValidationActivitiesLogRow = {
  log_file:
    "nccl-mpi_ink50-dh01-h18+ink50-dh01-h19+ink50-dh01-h20+ink50-dh01-h21+ink50-dh01-h22_127207.out",
  display_rack:
    "ink50-dh01-h18+ink50-dh01-h19+ink50-dh01-h20+ink50-dh01-h21+ink50-dh01-h22",
  last_job: "127207",
};

describe("logRowForActivity", () => {
  it("prefers the log whose job id matches a task, even when a superset log is listed first", () => {
    const a = activity({
      tasks: [
        {
          task_id: "t1",
          ordinal: 1,
          label: "nemo",
          scheduler_ref: "131920",
          scheduler_state: "COMPLETED",
          phase: "done",
          work_phase: "done",
          evidence: "squeue",
        },
      ],
    } as Partial<ValidationActivity>);
    // superset deliberately first — this is the ordering that produced the original bug
    expect(logRowForActivity(a, [supersetLog, ownLog])?.last_job).toBe("131920");
  });

  it("matches a job id embedded in the filename when last_job is unset", () => {
    const row = { ...ownLog, last_job: "-" };
    const a = activity({
      tasks: [
        {
          task_id: "t1",
          ordinal: 1,
          label: "nemo",
          scheduler_ref: "131920",
          scheduler_state: "COMPLETED",
          phase: "done",
          work_phase: "done",
          evidence: "squeue",
        },
      ],
    } as Partial<ValidationActivity>);
    expect(logRowForActivity(a, [supersetLog, row])?.log_file).toContain("131920");
  });

  it("falls back to an EXACT rack-set match when the activity has no tasks", () => {
    // This is the pre-fix failure case: no tasks (run-nemo-xrack.sh printed no jobid=).
    expect(logRowForActivity(activity(), [supersetLog, ownLog])?.last_job).toBe("131920");
  });

  it("never treats a UUID job_id as a Slurm job id", () => {
    const uuidOnly = activity({ job_id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" });
    // Must not throw or match nonsense; falls through to the exact rack-set match.
    expect(logRowForActivity(uuidOnly, [supersetLog, ownLog])?.last_job).toBe("131920");
  });

  it("breaks a coverage tie on the highest job id, not list order", () => {
    // Activity racks are a SUBSET of both candidates, so neither is an exact match.
    const a = activity({ racks: ["ink50-dh01-h19", "ink50-dh01-h20"] });
    const older: ValidationActivitiesLogRow = {
      log_file: "root-NEMO-ink50-dh01-h19+ink50-dh01-h20+ink50-dh01-h21-54N-100000.txt",
      display_rack: "ink50-dh01-h19+ink50-dh01-h20+ink50-dh01-h21",
      last_job: "100000",
    };
    const newer: ValidationActivitiesLogRow = {
      log_file: "root-NEMO-ink50-dh01-h19+ink50-dh01-h20+ink50-dh01-h22-54N-200000.txt",
      display_rack: "ink50-dh01-h19+ink50-dh01-h20+ink50-dh01-h22",
      last_job: "200000",
    };
    expect(logRowForActivity(a, [newer, older])?.last_job).toBe("200000");
    expect(logRowForActivity(a, [older, newer])?.last_job).toBe("200000");
  });

  it("returns null when nothing matches", () => {
    const a = activity({ rack: "", racks: ["ink50-dh01-zz99"] });
    expect(logRowForActivity(a, [supersetLog, ownLog])).toBeNull();
  });

  it("returns null for an empty or missing log list", () => {
    expect(logRowForActivity(activity(), [])).toBeNull();
    expect(logRowForActivity(activity(), undefined)).toBeNull();
  });
});
