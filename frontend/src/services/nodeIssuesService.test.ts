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
import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchDeepAudit, fetchDeepAuditOutput, fetchDeepAudits, startDeepAudit } from "./nodeIssuesService";

afterEach(() => vi.restoreAllMocks());

describe("deep audit result services", () => {
  it("loads terminal state and full durable output for the exact workflow run", async () => {
    const runId = "ae74eb2d-d38e-4a38-b6e3-0f1dfb474e1e";
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `/api/deep-audits/${runId}`) {
        return new Response(JSON.stringify({
          ok: true,
          run: {
            run_id: runId,
            workflow_id: "hardware-deep-audit",
            workflow_name: "Hardware Deep Audit",
            state: "completed",
            rack: "ink50-dh01-d01",
            target: "ink50-dh01-d01-32",
            started_at: "2026-08-13T19:17:24Z",
            updated_at: "2026-08-13T19:17:27Z",
            audits: {},
          },
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url === `/api/workflow-runs/${runId}/output/audit`) {
        return new Response(JSON.stringify({ ok: true, output: "d01-32: PASS" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      throw new Error(`unexpected request: ${url}`);
    });

    expect((await fetchDeepAudit(runId)).state).toBe("completed");
    expect(await fetchDeepAuditOutput(runId)).toContain("PASS");
  });

  it("uses the exact node when no rack is assigned", async () => {
    let launchBody: Record<string, unknown> = {};
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/workflows/hardware-deep-audit/run") {
        launchBody = JSON.parse(String(init?.body || "{}")) as Record<string, unknown>;
        return new Response(JSON.stringify({ ok: true, run_id: "run-1" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (url === "/api/deep-audits?kind=hardware&target=compute-001&limit=20") {
        return new Response(JSON.stringify({ ok: true, runs: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      throw new Error(`unexpected request: ${url}`);
    });

    await startDeepAudit({
      workflowId: "hardware-deep-audit",
      rack: "",
      target: "compute-001",
    });
    expect(launchBody).toEqual({ params: { target: "compute-001" } });
    await expect(
      fetchDeepAudits({ rack: "", target: "compute-001", kind: "hardware", limit: 20 }),
    ).resolves.toEqual([]);
  });
});
