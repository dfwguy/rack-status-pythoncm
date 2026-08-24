/* SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 */
// launchReview.test.ts — the client half of the reviewed-target binding (REQ-6).
// Author: Rafael M. Koike & Cursor Agent ("Ada")

import { beforeEach, describe, expect, it, vi } from "vitest";
import { LAUNCH_KIND_BY_URL, launchWithReview, prepareLaunch } from "./launchReview";

const fetchMock = vi.fn();
vi.mock("./apiFetch", () => ({
  apiFetch: (input: RequestInfo | URL, init?: RequestInit) => fetchMock(input, init),
}));

function jsonResponse(body: unknown, ok = true, status = 200): Response {
  return { ok, status, json: async () => body } as Response;
}

/** The bodies each recorded call was sent with, keyed by URL. */
function callsByUrl(): Record<string, Record<string, unknown>> {
  const out: Record<string, Record<string, unknown>> = {};
  for (const [url, init] of fetchMock.mock.calls) {
    out[String(url)] = JSON.parse(String((init as RequestInit)?.body ?? "{}"));
  }
  return out;
}

beforeEach(() => {
  fetchMock.mockReset();
});

describe("launchWithReview", () => {
  it("prepares first, then launches with the returned digest", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ ok: true, plan_hash: "abc123", plan: {} }))
      .mockResolvedValueOnce(jsonResponse({ ok: true, job_id: "j1" }));

    const res = await launchWithReview("/api/run-llmb-nccl-async", { rack: "p5-r01" });
    expect(res.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    const calls = callsByUrl();
    // prepare carries the kind derived from the URL, and not the launch's own plan_hash field
    expect(calls["/api/launch/prepare"]).toEqual({ kind: "llmb-nccl", rack: "p5-r01" });
    // the launch echoes the digest back
    expect(calls["/api/run-llmb-nccl-async"]).toEqual({ rack: "p5-r01", plan_hash: "abc123" });
  });

  it("sends the prepare and the launch the same body, so the digest describes what runs", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ ok: true, plan_hash: "h", plan: {} }))
      .mockResolvedValueOnce(jsonResponse({ ok: true, job_id: "j1" }));

    const body = { racks: ["a", "b"], dry_run: true, log_dir: "/logs", nccl_version: "2.30.7" };
    await launchWithReview("/api/run-nemo-xrack-async", body);

    const calls = callsByUrl();
    const { kind, ...prepared } = calls["/api/launch/prepare"];
    expect(kind).toBe("nemo-xrack");
    expect(prepared).toEqual(body);
    const { plan_hash, ...launched } = calls["/api/run-nemo-xrack-async"];
    expect(plan_hash).toBe("h");
    expect(launched).toEqual(body);
  });

  it("does not prepare for a URL outside the scheme, and posts it unchanged", async () => {
    // The preset path has its own stronger binding; double-gating it would break it.
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true, job_id: "j1" }));
    await launchWithReview("/api/run-validation-preset-async", { plan_hash: "own" });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(callsByUrl()["/api/run-validation-preset-async"]).toEqual({ plan_hash: "own" });
  });

  it("surfaces a prepare failure instead of launching anyway", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ ok: false, error: "invalid rack id" }, false, 400),
    );
    await expect(
      launchWithReview("/api/run-llmb-nccl-async", { rack: "nope" }),
    ).rejects.toThrow("invalid rack id");
    // and crucially: it did not fall through to the launch
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("treats a prepare with no digest as a failure", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true, plan: {} }));
    await expect(
      launchWithReview("/api/run-llmb-nccl-async", { rack: "p5-r01" }),
    ).rejects.toThrow();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("prepareLaunch", () => {
  it("returns null for an unmapped URL rather than throwing", async () => {
    expect(await prepareLaunch("/api/something-else", {})).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns the resolved plan for review", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        ok: true,
        plan_hash: "d1",
        plan: { target: "n[01-18]", rack: "p5-r01", command_display: "bash x.sh", log_dir: "/l" },
      }),
    );
    const review = await prepareLaunch("/api/run-nccl-fullrack-async", { rack: "p5-r01" });
    expect(review?.planHash).toBe("d1");
    // the resolved command is the thing the operator had no way to review before
    expect(review?.plan.command_display).toBe("bash x.sh");
    expect(review?.plan.target).toBe("n[01-18]");
  });
});

describe("LAUNCH_KIND_BY_URL", () => {
  it("covers every legacy launch endpoint the backend gates", () => {
    // Kept in step with _PREPARE_BY_KIND in backend/api/validation_runs.py. A URL missing here
    // would silently post without a digest and be refused with a confusing 409.
    expect(Object.keys(LAUNCH_KIND_BY_URL).sort()).toEqual([
      "/api/run-clusterkit-async",
      "/api/run-exemplar-llama-async",
      "/api/run-llama-async",
      "/api/run-llmb-nccl-async",
      "/api/run-mnubergemm-stress-async",
      "/api/run-nccl-fullrack-async",
      "/api/run-nemo-xrack-async",
      "/api/run-nvloom-async",
      "/api/run-validation",
      "/api/run-validation-async",
    ]);
  });

  it("does not map the preset path, which binds itself", () => {
    expect(LAUNCH_KIND_BY_URL["/api/run-validation-preset-async"]).toBeUndefined();
  });
});
