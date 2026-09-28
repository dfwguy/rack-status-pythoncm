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
import { fetchSlurmJson } from "./slurmHelpers";

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
