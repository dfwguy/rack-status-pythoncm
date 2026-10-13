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

/**
 * `fetchSlurmJson` backs every Slurm panel (queue, history, node reasons, job detail). It used a
 * raw `fetch`, so all of them failed once the access token expired.
 */
import { afterEach, describe, expect, it } from "vitest";
import { fetchSlurmJson, parseSlurmJobStateFromScontrolText, slurmJobStateLabel } from "./slurmHelpers";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("fetchSlurmJson", () => {
  it("renews an expired session and returns the retried response", async () => {
    const calls: string[] = [];
    let served = 0;
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = String(input);
      calls.push(url);
      if (url.includes("/api/auth/refresh")) return json({ ok: true });
      served += 1;
      return served === 1
        ? json({ error: "authentication required" }, 401)
        : json({ ok: true, stdout: "JOBID NAME\n42 nccl\n", returncode: 0 });
    }) as typeof fetch;

    const { res, data } = await fetchSlurmJson("/api/scheduler-jobs");

    expect(res.status).toBe(200);
    expect(data.stdout).toContain("42 nccl");
    expect(calls).toEqual(["/api/scheduler-jobs", "/api/auth/refresh", "/api/scheduler-jobs"]);
  });

  it("passes the request options through", async () => {
    let seen: RequestInit | undefined;
    globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
      seen = init;
      return json({ ok: true });
    }) as typeof fetch;

    await fetchSlurmJson("/api/scheduler-cancel", { method: "POST", body: JSON.stringify({ job_id: "42" }) });

    expect(seen?.method).toBe("POST");
    expect(seen?.body).toBe('{"job_id":"42"}');
    expect(seen?.credentials).toBe("same-origin");
  });

  it("reports a non-JSON body with its status", async () => {
    globalThis.fetch = (async () => new Response("<html>gateway</html>", { status: 502 })) as typeof fetch;

    await expect(fetchSlurmJson("/api/scheduler-jobs")).rejects.toThrow(/Invalid JSON \(HTTP 502\)/);
  });
});

describe("parseSlurmJobStateFromScontrolText", () => {
  it("reads a pending job's state and why it waits", () => {
    const text = "JobId=194782 JobName=NEMO-36N\n   JobState=PENDING Reason=ReqNodeNotAvail,_May_be_reserved_for_other_job Dependency=(null)";
    expect(parseSlurmJobStateFromScontrolText(text)).toEqual({
      state: "PENDING",
      reason: "ReqNodeNotAvail, May be reserved for other job",
    });
  });

  it("reads a running job with no reason", () => {
    expect(parseSlurmJobStateFromScontrolText("   JobState=RUNNING Reason=None Dependency=(null)")).toEqual({
      state: "RUNNING",
      reason: "",
    });
  });

  it("says nothing for text without a state", () => {
    expect(parseSlurmJobStateFromScontrolText("")).toEqual({ state: "", reason: "" });
  });
});

describe("slurmJobStateLabel", () => {
  it.each([
    ["PD", "Pending"],
    ["R", "Running"],
    ["CG", "Completing"],
    ["CA", "Cancelled"],
    ["F", "Failed"],
    ["NF", "Node fail"],
    ["OOM", "Out of memory"],
    ["S", "Suspended"],
    ["ST", "Stopped"],
  ])("spells squeue's %s as %s", (code, word) => {
    expect(slurmJobStateLabel(code)).toBe(word);
  });

  it("reads a long state name too", () => {
    expect(slurmJobStateLabel("PENDING")).toBe("Pending");
    expect(slurmJobStateLabel("NODE_FAIL")).toBe("Node fail");
  });

  it("leaves what it does not know as Slurm wrote it", () => {
    expect(slurmJobStateLabel("XZ")).toBe("XZ");
    expect(slurmJobStateLabel("")).toBe("");
  });
});
