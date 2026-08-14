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
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SingleRackView } from "./SingleRackView";
import type { ClusterSnapshotMeta, RackStatusResponse } from "../types/api";

vi.mock("../components/RackTable", () => ({
  RackTable: ({ rows }: { rows: RackStatusResponse["rows"] }) => (
    <table>
      <tbody>
        {rows.map((row) => (
          <tr key={row.rack}>
            <td>{row.rack}</td>
          </tr>
        ))}
      </tbody>
    </table>
  ),
}));

const snapshot = (
  state: ClusterSnapshotMeta["state"],
  error: string | null = null
): ClusterSnapshotMeta => ({
  id: state === "initializing" || state === "failed" ? null : "snap-1",
  state,
  cluster_id: "cluster-a",
  log_root: "/logs",
  configuration_generation: 1,
  last_attempt_at: "2026-07-22T15:00:00Z",
  last_success_at: state === "initializing" || state === "failed" ? null : "2026-07-22T14:59:00Z",
  age_seconds: state === "initializing" || state === "failed" ? null : 60,
  last_error: error,
});

const status = (
  state: ClusterSnapshotMeta["state"],
  rows: RackStatusResponse["rows"]
): RackStatusResponse => ({
  ok: true,
  returncode: 0,
  updated_at: "2026-07-22T14:59:00Z",
  log_dir: "/logs",
  script: "collector",
  stderr: "",
  legend: "",
  rows,
  summary: { done: rows.length, fail: 0, ongoing: 0, pending: 0 },
  footnotes: [],
  comments: {},
  physical_rf0: {},
  native_workers: { mode: "test" },
  snapshot: snapshot(state, state === "stale" || state === "failed" ? "inventory failed" : null),
});

const rackRow = {
  rack: "p5-r01",
  nemo: "done",
  last_job: "1",
  sinfo: "idle=16",
  ct_down_unk: "",
};

describe("SingleRackView snapshot states", () => {
  it("shows page shell, loading banner, and table-area placeholder while initializing", () => {
    render(<SingleRackView data={status("initializing", [])} loading={false} error={null} />);
    expect(screen.getByText("Rack Validation Status")).toBeInTheDocument();
    expect(screen.getByText("Loading cluster inventory…")).toBeInTheDocument();
    expect(screen.getByLabelText("Loading rack inventory")).toBeInTheDocument();
    expect(screen.getByRole("status")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows unavailable error and retry without loading placeholder", () => {
    const retry = vi.fn();
    render(
      <SingleRackView
        data={status("failed", [])}
        loading={false}
        error={null}
        onRetry={retry}
      />
    );
    expect(screen.getByText("Cluster inventory unavailable")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry inventory" }));
    expect(retry).toHaveBeenCalledOnce();
    expect(screen.queryByLabelText("Loading rack inventory")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByText(/will appear when/i)).not.toBeInTheDocument();
    expect(screen.getByText(/Rack table unavailable/i)).toBeInTheDocument();
    expect(document.querySelectorAll("[class*='nv-skeleton']").length).toBe(0);
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("shows stale warning and last-good rack rows", () => {
    render(<SingleRackView data={status("stale", [rackRow])} loading={false} error={null} />);
    expect(screen.getByText(/Showing stale cluster inventory/)).toBeInTheDocument();
    expect(screen.getByText("p5-r01")).toBeInTheDocument();
    expect(screen.queryByLabelText("Loading rack inventory")).not.toBeInTheDocument();
  });

  it.each([
    ["fresh non-empty", [rackRow]],
    ["fresh confirmed empty", []],
  ])("renders %s state with table content only", (_label, rows) => {
    render(<SingleRackView data={status("fresh", rows)} loading={false} error={null} />);
    expect(screen.queryByText("Cluster inventory unavailable")).not.toBeInTheDocument();
    expect(screen.queryByText("Loading cluster inventory…")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Loading rack inventory")).not.toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
    for (const row of rows) {
      expect(screen.getByText(row.rack)).toBeInTheDocument();
    }
  });
});
