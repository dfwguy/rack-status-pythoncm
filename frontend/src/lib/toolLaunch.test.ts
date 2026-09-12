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
import type { Tool, ToolLaunch } from "../types/catalog";
import { GENERIC_JOB_STATUS, planToolLaunch, statusUrlFor } from "./toolLaunch";

function tool(launch: Partial<ToolLaunch>, over: Partial<Tool> = {}): Tool {
  return {
    id: "t",
    name: "Test Tool",
    category: "validation",
    script: "t.sh",
    env_key: "",
    params: [],
    compatible: { match: "all", capabilities: [], platform_ids: [] },
    surfaces: [],
    execution: { mode: "async", target: "rack" },
    description: "",
    ai_context: "",
    status: "ga",
    source: "seed",
    subtitle: "",
    button_label: "",
    icon: "",
    image: "",
    destructive: false,
    order: 100,
    launch: {
      kind: "none",
      endpoint: "",
      preset_id: "",
      dialog: "",
      scopes: [],
      body: {},
      confirm: false,
      min_nodes: 0,
      max_nodes: 0,
      ...launch,
    },
    ...over,
  };
}

describe("planToolLaunch", () => {
  it("reads a preset binding, carrying the scopes the recipe supports", () => {
    const plan = planToolLaunch(
      tool({ kind: "validation-preset", preset_id: "nccl-2.31.2", scopes: ["node", "rack"] }),
    );
    expect(plan).toMatchObject({
      kind: "validation-preset",
      presetId: "nccl-2.31.2",
      scopes: ["node", "rack"],
    });
  });

  it("reads an async binding with its options dialog and body", () => {
    const plan = planToolLaunch(
      tool({
        kind: "async-job",
        endpoint: "/api/run-nccl-fullrack-async",
        dialog: "launch-options",
        body: { show_nccl_version: true },
      }),
    );
    expect(plan).toMatchObject({
      kind: "async-job",
      endpoint: "/api/run-nccl-fullrack-async",
      dialog: "launch-options",
      body: { show_nccl_version: true },
    });
  });

  it("forces a confirm step for a destructive tool even when the document did not ask", () => {
    const plan = planToolLaunch(
      tool({ kind: "async-job", endpoint: "/api/run-x-async" }, { destructive: true }),
    );
    expect(plan).toMatchObject({ kind: "async-job", confirm: true });
  });

  it.each([
    ["https://evil.example/api/run-x-async", "an absolute URL"],
    ["//evil.example/api/x", "a protocol-relative URL"],
    ["/api/../../etc/passwd", "a traversal"],
    ["/run-x-async", "a path outside /api"],
  ])("refuses %s (%s)", (endpoint) => {
    // The backend validates this on write; a store edited by hand on the box skips that path,
    // so the value reaching the browser is checked again here.
    const plan = planToolLaunch(tool({ kind: "async-job", endpoint }));
    expect(plan.kind).toBe("unsupported");
  });

  it("reports an unbound tool rather than returning nothing", () => {
    expect(planToolLaunch(tool({ kind: "none" }))).toMatchObject({ kind: "unsupported" });
  });

  it("reports a preset binding with no recipe", () => {
    expect(planToolLaunch(tool({ kind: "validation-preset" }))).toMatchObject({
      kind: "unsupported",
    });
  });

  it("reports a launch kind this build does not know, naming the likely cause", () => {
    // Real state, not a defensive branch: the catalog is served by a backend that can be
    // newer than the bundle the browser is running.
    const plan = planToolLaunch(tool({ kind: "teleport" as ToolLaunch["kind"] }));
    expect(plan.kind).toBe("unsupported");
    expect(plan.kind === "unsupported" && plan.reason).toMatch(/older than the catalog/);
  });

  it("always carries a label, so an unsupported card can still name itself", () => {
    expect(planToolLaunch(tool({ kind: "none" })).label).toBe("Test Tool");
  });
});

describe("statusUrlFor", () => {
  it("pairs a run-* launch with its own status route", () => {
    expect(statusUrlFor("/api/run-nvloom-async", "abc")).toBe(
      "/api/run-nvloom-status?job_id=abc",
    );
  });

  it("sends every other async action to the generic poll route", () => {
    // nvlink-preflight has no -status route of its own; deriving one would poll a 404, which
    // presents as a job that never finishes rather than as an error.
    expect(statusUrlFor("/api/nvlink-preflight-async", "abc")).toBe(
      `${GENERIC_JOB_STATUS}?job_id=abc`,
    );
    expect(statusUrlFor("/api/gpu-reset-async", "abc")).toBe(`${GENERIC_JOB_STATUS}?job_id=abc`);
  });

  it("escapes the job id", () => {
    expect(statusUrlFor("/api/run-nvloom-async", "a b&c")).toContain("job_id=a%20b%26c");
  });
});
