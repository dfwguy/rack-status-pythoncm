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
import { render, screen } from "@testing-library/react";
import { IssueHealthOverview } from "./IssueHealthOverview";

describe("IssueHealthOverview", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses validation/Slurm post-run history rather than the Google punchcard endpoint", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      void input;
      return new Response(JSON.stringify({
        ok: true,
        generated_at: "2026-08-13T15:00:00Z",
        history_path: "/app/var/history/post_run_nodes.json",
        summary: { nodes: 2, participations: 7, attributed_failures: 1, flagged_nodes: 0, drained_nodes: 0 },
        racks: [],
        nodes: [],
        refresh: { activities: 4, merge: {}, accounting_error: "", inventory_error: "" },
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<IssueHealthOverview />);

    expect((await screen.findAllByText("Validation participations")).length).toBeGreaterThan(0);
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/post-run-node-report");
    expect(String(fetchMock.mock.calls[0][0])).not.toContain("/api/node-issues");
  });
});
