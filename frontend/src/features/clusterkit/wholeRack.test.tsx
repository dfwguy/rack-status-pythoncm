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
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RunDetails } from "./RunDetails";
import { RunComposer } from "./RunComposer";
import * as service from "./service";
import { completed, preview, readiness } from "./testing/fixtures";
import type { RackDirectory } from "./types";

vi.mock("./service", () => ({
  confirmRun: vi.fn(), previewRun: vi.fn(), getRackDirectory: vi.fn(), getInventory: vi.fn(), getArtifacts: vi.fn(), downloadArtifact: vi.fn(),
}));
const nodes = Array.from({ length: 18 }, (_, index) => "compute-" + (index + 1));
const directory: RackDirectory = {
  racks: [18, 4].map((count, index) => ({
    name: "R" + (index + 1), node_count: count, nodes: nodes.slice(0, count),
    counts: { idle: count, busy: 0, drain: 0, down: 0, reserved: 0, unknown: 0 },
    allowed_node_count: count, allowed_node_counts: [1, 2, 4, 8].filter((n) => n <= count),
    authoritative: true, test_enabled: true, reason: "",
    whole_rack_enabled: true, whole_rack_reason: "",
  })),
  nodes: [], snapshot: { state: "fresh", age_seconds: 1 }, node_sources: { scheduler: "slurm", orchestrator: "bcm" },
  unplaced_node_count: 0,
};
const ready = { ...readiness, rack_inventory_enabled: true, whole_rack_enabled: true, max_nodes: 18 };
function chooseCoverage(text: string) {
  const control = screen.getByRole("combobox", { name: "Coverage" });
  const id = control.getAttribute("aria-controls");
  const option = screen.getAllByRole("option", { hidden: true }).find((node) =>
    node.closest("#" + id) !== null && node.textContent === text);
  if (!option) throw new Error("Missing coverage option " + text);
  fireEvent.click(option);
}
beforeEach(() => {
  vi.mocked(service.getRackDirectory).mockResolvedValue(structuredClone(directory));
  vi.mocked(service.getInventory).mockImplementation(async (_signal, group) => ({
    group, captured_at: "now", nodes: nodes.slice(0, group === "R1" ? 18 : 4).map((node) =>
      ({ node, states: ["IDLE"], partitions: ["validation"], allowed: true })),
  }));
  vi.mocked(service.previewRun).mockResolvedValue({
    ...structuredClone(preview), plan: { ...structuredClone(preview.plan), scope: "rack",
      nodes, node_count: 18, expected_pair_count: 153 },
  });
  vi.mocked(service.confirmRun).mockResolvedValue(completed);
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });
async function selected() {
  render(<RunComposer readiness={ready} inventory={null} onSubmit={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Select rack R1" }));
  await screen.findByText(/18 permitted idle nodes in R1/);
  chooseCoverage("Whole rack");
}

it("previews the entire authoritative rack and displays all 153 expected pairs", async () => {
  await selected();
  expect(screen.getByRole("status")).toHaveTextContent("18 nodes / 153 pairs");
  expect(screen.queryByLabelText("Node count")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /Preview run/i }));
  await waitFor(() => expect(service.previewRun).toHaveBeenCalledOnce());
  expect(vi.mocked(service.previewRun).mock.calls[0][0]).toEqual({
    scope: "rack", group: "R1", node_count: 18, path_comparison: true,
  });
  expect(await screen.findByText("18 nodes / 153 pairs")).toBeInTheDocument();
  expect(service.confirmRun).not.toHaveBeenCalled();
});

it("blocks whole-rack preview if any member is busy", async () => {
  vi.mocked(service.getInventory).mockResolvedValue({
    group: "R1", captured_at: "now", nodes: nodes.map((node, index) =>
      ({ node, states: [index === 17 ? "ALLOCATED" : "IDLE"], partitions: ["validation"] })),
  });
  render(<RunComposer readiness={ready} inventory={null} onSubmit={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Select rack R1" }));
  await screen.findByText(/17 permitted idle nodes/);
  chooseCoverage("Whole rack");
  expect(screen.getByRole("button", { name: /Preview run/i })).toBeDisabled();
  expect(service.previewRun).not.toHaveBeenCalled();
  expect(screen.getByRole("status")).toHaveTextContent("All mapped nodes must be idle");
});

it("recomputes rack size and clears the preview when the rack changes", async () => {
  await selected();
  fireEvent.click(screen.getByRole("button", { name: /Preview run/i }));
  await screen.findByRole("button", { name: /Confirm & run/i });
  fireEvent.click(screen.getByRole("button", { name: "Select rack R2" }));
  await screen.findByText(/4 permitted idle nodes in R2/);
  expect(screen.getByRole("status")).toHaveTextContent("4 nodes / 6 pairs");
  expect(screen.queryByRole("button", { name: /Confirm & run/i })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /Preview run/i }));
  await waitFor(() => expect(service.previewRun).toHaveBeenCalledTimes(2));
  expect(vi.mocked(service.previewRun).mock.calls[1][0]).toMatchObject({ scope: "rack", group: "R2", node_count: 4 });
});

it("retains existing subset mode and discards a whole-rack preview when switching back", async () => {
  await selected();
  fireEvent.click(screen.getByRole("button", { name: /Preview run/i }));
  await screen.findByRole("button", { name: /Confirm & run/i });
  chooseCoverage("Selected size");
  expect(screen.getByLabelText("Node count")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Confirm & run/i })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /Preview run/i }));
  await waitFor(() => expect(service.previewRun).toHaveBeenCalledTimes(2));
  expect(vi.mocked(service.previewRun).mock.calls[1][0]).toMatchObject({ scope: "group", node_count: 2 });
});

it("does not expose whole-rack mode when disabled by the administrator", async () => {
  render(<RunComposer readiness={{ ...ready, whole_rack_enabled: false }} inventory={null} onSubmit={vi.fn()} />);
  await screen.findByRole("button", { name: "Select rack R1" });
  expect(screen.queryByRole("combobox", { name: "Coverage" })).not.toBeInTheDocument();
});

it("shows policy reasons and disables whole-rack choice when some members are disallowed", async () => {
  const blocked = structuredClone(directory);
  blocked.racks[0].whole_rack_enabled = false;
  blocked.racks[0].whole_rack_reason = "Every mapped rack member must be permitted by the administrator";
  vi.mocked(service.getRackDirectory).mockResolvedValue(blocked);
  render(<RunComposer readiness={ready} inventory={null} onSubmit={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Select rack R1" }));
  await screen.findByText(/Every mapped rack member must be permitted/);
  const control = screen.getByRole("combobox", { name: "Coverage" });
  const id = control.getAttribute("aria-controls");
  const option = screen.getAllByRole("option", { hidden: true }).find((node) =>
    node.closest("#" + id) !== null && node.textContent === "Whole rack");
  expect(option).toBeDefined();
  fireEvent.click(option!);
  expect(screen.getByLabelText("Node count")).toBeInTheDocument();
  expect(screen.queryByText(/Entire rack:/)).not.toBeInTheDocument();
});

it("uses the rack browser for authoritative manual rack mappings too", async () => {
  render(<RunComposer readiness={{ ...ready, rack_inventory_enabled: false, groups: [{
    name: "R1", kind: "rack", authoritative: true, nodes, source: "Administrator mapping",
  }] }} inventory={null} onSubmit={vi.fn()} />);
  expect(await screen.findByRole("button", { name: "Select rack R1" })).toBeInTheDocument();
  expect(service.getInventory).not.toHaveBeenCalled();
});

it("renders all 153 pairs per metric and filters a full-rack result without truncation", () => {
  const run = structuredClone(completed);
  run.activity.nodes = nodes;
  run.activity.node_count = 18;
  run.activity.expected_pair_count = 153;
  run.summary!.nodes = nodes;
  run.summary!.expected_pair_count = 153;
  const pairs = nodes.flatMap((node, index) => nodes.slice(index + 1).map((other) => [node, other]));
  for (const stage of Object.values(run.summary!.stages!)) {
    for (const metric of Object.values(stage.metrics!)) {
      metric.observed_pair_count = 153;
      metric.pairs = pairs.map((pair, index) => ({ nodes: pair, value: index + 1 }));
    }
  }
  render(<RunDetails run={run} activityId={run.activity_id} error="" submitting={false} refresh={vi.fn()} />);
  expect(screen.getAllByText("153 / 153")).toHaveLength(4);
  const tables = screen.getAllByRole("table", { name: "bandwidth (MB/s)" });
  expect(tables).toHaveLength(2);
  for (const table of tables) expect(within(table).getAllByRole("row")).toHaveLength(154);
  fireEvent.change(screen.getByLabelText("Filter node pairs"), { target: { value: "compute-18" } });
  for (const table of tables) expect(within(table).getAllByRole("row")).toHaveLength(18);
  expect(screen.getAllByText("UNVERIFIED").length).toBeGreaterThan(0);
});
