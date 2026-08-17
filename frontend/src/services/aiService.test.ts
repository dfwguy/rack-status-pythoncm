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
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("../lib/apiFetch", () => ({ apiFetch: vi.fn() }));

import { apiFetch } from "../lib/apiFetch";
import { streamChat, type ToolEvent } from "./aiService";

function sseResponse(lines: string[]): Response {
  const enc = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const l of lines) controller.enqueue(enc.encode(l));
      controller.close();
    },
  });
  return new Response(stream, { status: 200 });
}

async function waitFor(pred: () => boolean, ms = 1000) {
  const start = Date.now();
  while (!pred() && Date.now() - start < ms) {
    await new Promise((r) => setTimeout(r, 5));
  }
  if (!pred()) throw new Error("waitFor timed out");
}

describe("streamChat SSE parsing", () => {
  beforeEach(() => vi.clearAllMocks());

  it("routes tokens, tool events, approval, and done", async () => {
    (apiFetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      sseResponse([
        'data: {"content":"Hello "}\n',
        'data: {"content":"world"}\n',
        'data: {"type":"tool_call","tool":"get_cluster_overview","category":"read","args":{}}\n',
        'data: {"type":"tool_result","tool":"get_cluster_overview","ok":true,"summary":"8 racks"}\n',
        'data: {"type":"approval_required","run_id":"r1","call_id":"c1","tool":"scancel_job","args":{"job_id":"214"},"summary":"scancel_job(job_id=214)"}\n',
        "data: [DONE]\n",
      ]),
    );

    const chunks: string[] = [];
    const events: ToolEvent[] = [];
    let done = false;
    let error = "";

    streamChat([{ role: "user", content: "hi" }], { page: "dashboard" }, {
      onChunk: (c) => chunks.push(c),
      onEvent: (e) => events.push(e),
      onDone: () => {
        done = true;
      },
      onError: (e) => {
        error = e;
      },
    });

    await waitFor(() => done);
    expect(error).toBe("");
    expect(chunks.join("")).toBe("Hello world");
    const types = events.map((e) => e.type);
    expect(types).toEqual(["tool_call", "tool_result", "approval_required"]);
    const appr = events.find((e) => e.type === "approval_required")!;
    expect(appr.run_id).toBe("r1");
    expect(appr.call_id).toBe("c1");
    expect(appr.tool).toBe("scancel_job");
  });

  it("surfaces error events", async () => {
    (apiFetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      sseResponse(['data: {"error":"boom"}\n']),
    );
    let error = "";
    let done = false;
    streamChat([{ role: "user", content: "hi" }], { page: "dashboard" }, {
      onChunk: () => {},
      onDone: () => {
        done = true;
      },
      onError: (e) => {
        error = e;
      },
    });
    await waitFor(() => error !== "" || done);
    expect(error).toBe("boom");
  });
});
