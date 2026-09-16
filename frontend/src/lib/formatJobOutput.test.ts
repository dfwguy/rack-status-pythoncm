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
import { formatJobStatusText, formatSchedulerCommandResult, formatSyncJsonResult } from "./formatJobOutput";

describe("scheduler command output", () => {
  it("formats PythonCM direct-execution drain results", () => {
    expect(
      formatSyncJsonResult("Drain nodes", "rack-01", {
        ok: true,
        command_display: "pythoncm: set category drained",
        returncode: 0,
        stdout: "node01 drained",
      }),
    ).toBe("Drain completed for rack-01.\n\npythoncm: set category drained\nexit 0\nstdout:\nnode01 drained");
  });

  it("formats scheduler-neutral cancellation results", () => {
    expect(
      formatSyncJsonResult("Cancel job", "rack-01", {
        ok: true,
        job_id: "run-42",
        command_display: "pythoncm: terminate run-42",
        returncode: 0,
      }),
    ).toContain("Cancellation completed for job run-42.");
  });

  it("keeps command result formatting independent of provider", () => {
    expect(formatSchedulerCommandResult({ command_display: "direct node01", returncode: 7, stderr: "failed" })).toBe(
      "direct node01\nexit 7\nstderr:\nfailed",
    );
  });

  it("keeps scheduler job ids and wrapper tails visible when a validation exits with error", () => {
    const text = formatJobStatusText({
      phase: "error",
      kind: "nccl-fullrack",
      job_id: "f644d3bc-9f2c-4470-8d49-68a2a5fe287b",
      activity_id: "f644d3bc-9f2c-4470-8d49-68a2a5fe287b",
      error: "validation script exited non-zero",
      tasks: [
        { scheduler_ref: "55430" },
        { scheduler_ref: "55432" },
      ],
      stdout_tail: "SUBMITTED jobid=55432",
      stderr_tail: "wrapper rc=143",
    });

    expect(text).toContain("Scheduler Job IDs: 55430, 55432");
    expect(text).toContain("Activity ID: f644d3bc-9f2c-4470-8d49-68a2a5fe287b");
    expect(text).toContain("stdout:\nSUBMITTED jobid=55432");
    expect(text).toContain("stderr:\nwrapper rc=143");
  });
});
