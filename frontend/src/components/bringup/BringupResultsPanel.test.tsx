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
import { describe, expect, it, vi, beforeEach } from "vitest";
import { BringupResultsPanel } from "./BringupResultsPanel";
import type { BringupNodeSummary } from "../../types/bringup";

// Isolate the panel from the fetch hook and the heavy dialogs.
const mockUse = vi.fn();
vi.mock("../../hooks/useBringupNode", () => ({
  useBringupNode: (node: string | null) => mockUse(node),
}));
const tailPayloads: unknown[] = [];
vi.mock("../TailLogDialog", () => ({
  TailLogDialog: ({ open, title, payload }: { open: boolean; title: string; payload: unknown }) => {
    if (open) tailPayloads.push(payload);
    return open ? <div data-testid="tail-dialog">{title}</div> : null;
  },
}));
vi.mock("../JobLogContentDialog", () => ({
  JobLogContentDialog: ({ open, title, path }: { open: boolean; title: string; path: string | null }) =>
    open ? <div data-testid="content-dialog" data-path={path ?? ""}>{title}</div> : null,
}));
vi.mock("../ParserResultsDialog", () => ({
  ParserResultsDialog: ({ open, logPath }: { open: boolean; logPath?: string }) =>
    open ? <div data-testid="parser-dialog">{logPath}</div> : null,
}));

const refresh = vi.fn();

function summary(overrides: Partial<BringupNodeSummary> = {}): BringupNodeSummary {
  return {
    node: "ink50-dh01-h01-11",
    present: true,
    slurm_state: "idle",
    home: "/home/cmods/bringup",
    jobs: [
      { name: "dcgm", base: "dcgm", status: "DONE", reason: "", metric: null, metric_label: null },
      { name: "allreduce", base: "allreduce", status: "DONE", reason: "", metric: "688.6", metric_label: "busbw GB/s" },
      { name: "allreduce-nonvlink", base: "allreduce-nonvlink", status: "FAIL", reason: "below floor (43): 2.28", metric: "2.28", metric_label: "busbw GB/s" },
      { name: "alltoall", base: "alltoall", status: "NOT_RUN", reason: "", metric: null, metric_label: null },
      { name: "alltoall-nonvlink", base: "alltoall-nonvlink", status: "RUNNING", reason: "job is on the node now", metric: null, metric_label: "busbw GB/s" },
    ],
    ...overrides,
  };
}

function setHook(state: Partial<ReturnType<typeof mockUse>>) {
  mockUse.mockReturnValue({ data: null, loading: false, error: null, refresh, ...state });
}

beforeEach(() => {
  mockUse.mockReset();
  refresh.mockReset();
  tailPayloads.length = 0;
});

describe("BringupResultsPanel", () => {
  it("renders a row per job with status tags and metrics", () => {
    setHook({ data: summary() });
    render(<BringupResultsPanel node="ink50-dh01-h01-11" logDir="/logs" />);
    expect(screen.getByText("Bring-up results")).toBeInTheDocument();
    expect(screen.getByText("dcgm")).toBeInTheDocument();
    expect(screen.getByText("FAIL")).toBeInTheDocument();
    expect(screen.getByText("688.6 busbw GB/s")).toBeInTheDocument();
    expect(screen.getByText("below floor (43): 2.28")).toBeInTheDocument();
  });

  it("shows a loading indicator while fetching", () => {
    setHook({ loading: true });
    render(<BringupResultsPanel node="n1" />);
    expect(screen.getByLabelText("Loading bring-up results")).toBeInTheDocument();
  });

  it("shows a warning banner on error and a friendlier note when disabled", () => {
    setHook({ error: "boom" });
    const { rerender } = render(<BringupResultsPanel node="n1" />);
    expect(screen.getByText(/Could not load bring-up results/)).toBeInTheDocument();
    setHook({ error: "bring-up integration disabled or unavailable" });
    rerender(<BringupResultsPanel node="n1" />);
    expect(screen.getByText(/not available for this deployment/)).toBeInTheDocument();
  });

  it("shows an empty note when there are no jobs", () => {
    setHook({ data: summary({ jobs: [] }) });
    render(<BringupResultsPanel node="n1" />);
    expect(screen.getByText(/No bring-up results recorded/)).toBeInTheDocument();
  });

  it("refreshes on demand", () => {
    setHook({ data: summary() });
    render(<BringupResultsPanel node="n1" />);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("shows the whole file (content dialog) for a finished job, not a tail", () => {
    setHook({ data: summary() });
    render(<BringupResultsPanel node="ink50-dh01-h01-11" logDir="/logs" />);
    fireEvent.click(screen.getByRole("button", { name: "Open allreduce log" }));
    const dlg = screen.getByTestId("content-dialog");
    expect(dlg).toHaveAttribute("data-path", "/home/cmods/bringup/results/ink50-dh01-h01-11/allreduce.txt");
    expect(screen.queryByTestId("tail-dialog")).not.toBeInTheDocument();
  });

  it("live-tails a RUNNING job instead of a static read", () => {
    setHook({ data: summary() });
    render(<BringupResultsPanel node="ink50-dh01-h01-11" logDir="/logs" />);
    fireEvent.click(screen.getByRole("button", { name: "Open alltoall-nonvlink log" }));
    expect(screen.getByTestId("tail-dialog")).toBeInTheDocument();
    expect(screen.queryByTestId("content-dialog")).not.toBeInTheDocument();
  });

  it("keeps the RUNNING tail payload identity stable across parent re-renders", () => {
    // Regression: an inline payload object made TailLogDialog restart its tail -f
    // stream on every parent poll (rack-locks ~5s), flickering to "(stopped)".
    setHook({ data: summary() });
    const props = { node: "ink50-dh01-h01-11", logDir: "/logs" };
    const { rerender } = render(<BringupResultsPanel {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "Open alltoall-nonvlink log" }));
    rerender(<BringupResultsPanel {...props} />); // simulate a parent re-render
    rerender(<BringupResultsPanel {...props} />);
    const seen = tailPayloads.filter((p) => p !== null);
    expect(seen.length).toBeGreaterThanOrEqual(2);
    // Same object reference every render -> the stream effect never re-fires.
    expect(seen.every((p) => p === seen[0])).toBe(true);
  });

  it("opens charts only for NCCL jobs, pointed at the node log path", () => {
    setHook({ data: summary() });
    render(<BringupResultsPanel node="ink50-dh01-h01-11" logDir="/logs" />);
    // dcgm is not an NCCL chart job -> its charts button is disabled.
    expect(screen.getByRole("button", { name: "Open dcgm charts" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Open allreduce charts" }));
    const dlg = screen.getByTestId("parser-dialog");
    expect(dlg).toHaveTextContent("/home/cmods/bringup/results/ink50-dh01-h01-11/allreduce.txt");
  });
});
