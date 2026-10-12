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
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { AnalyticsView } from "./AnalyticsView";

// The multirack section is under test, not the charts: canvas rendering needs a real browser.
vi.mock("react-chartjs-2", () => ({ Line: () => null, Bar: () => null }));
const { platform } = vi.hoisted(() => ({
  platform: { current: { geometry: { trays_per_rack: 18, gpus_per_node: 0 } } },
}));
vi.mock("../context/ActiveClusterContext", () => ({
  useActiveCluster: () => ({ activeCluster: null, activePlatform: platform.current }),
}));

// jsdom has no ResizeObserver, which the page's responsive tables reach for.
class NoopResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", NoopResizeObserver);

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("../lib/apiFetch", () => ({ apiFetch }));

function respondWith(multirack: Record<string, unknown>, singleRack: Record<string, unknown> | null = null) {
  apiFetch.mockImplementation(async (url: string) => {
    if (String(url).startsWith("/api/analytics/last")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          ok: true,
          cached: true,
          analyzed_at: "2026-10-12T00:00:00Z",
          single_rack: singleRack,
          multirack,
        }),
      };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  });
}

const NEMOTRON_1RACK = { n_racks: 1, n_logs: 36, mean_tflops_per_gpu: 1984.6, mean_step_s: 4.15 };

function renderAnalytics() {
  return render(
    <MemoryRouter>
      <AnalyticsView logDir="/logs" rows={[]} status={null} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  apiFetch.mockReset();
  platform.current = { geometry: { trays_per_rack: 18, gpus_per_node: 0 } };
});

describe("AnalyticsView multirack scaling", () => {
  it("shows NCCL peaks by node count with the equivalent racks, so a passed 2-rack run is visible", async () => {
    respondWith({
      nccl_series_by_log: [],
      nccl_scaling: [
        {
          num_nodes: 18, n_logs: 12, n_logs_no_metrics: 0, logs_no_metrics: [],
          mean_all_reduce_gbps: 690, mean_all_gather_gbps: 520.5, mean_alltoall_gbps: 140,
          mean_reduce_scatter_gbps: null, mean_sendrecv_gbps: null,
        },
        {
          num_nodes: 36, num_gpus: 144, n_logs: 1, n_logs_no_metrics: 0, logs_no_metrics: [],
          mean_all_reduce_gbps: 798, mean_all_gather_gbps: null, mean_alltoall_gbps: 135,
          mean_reduce_scatter_gbps: null, mean_sendrecv_gbps: null,
        },
      ],
      nemotron_scaling: [NEMOTRON_1RACK],
      nemotron_throughput_scaling: [],
    });
    renderAnalytics();
    const table = await screen.findByRole("table", { name: "NCCL scaling" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    // 36 nodes at 18 per rack is 2 racks, and its all-reduce peak is on the page.
    expect(within(rows[1]).getAllByRole("cell").map((c) => c.textContent)).toEqual(
      ["36", "144", "2", "1", "798", "—", "135", "—", "—"],
    );
    // The 18-node row's log did not state its GPUs and there is no platform figure: a dash, not a guess.
    expect(within(rows[0]).getAllByRole("cell")[1]).toHaveTextContent("—");
    expect(within(rows[0]).getAllByRole("cell")[2]).toHaveTextContent("1");
  });

  it("reports an NCCL log with no measured collective instead of averaging it in", async () => {
    respondWith({
      nccl_series_by_log: [],
      nccl_scaling: [{
        num_nodes: 30, n_logs: 0, n_logs_no_metrics: 2, logs_no_metrics: ["a.out", "b.out"],
        mean_all_reduce_gbps: null, mean_all_gather_gbps: null, mean_alltoall_gbps: null,
        mean_reduce_scatter_gbps: null, mean_sendrecv_gbps: null,
      }],
      nemotron_scaling: [NEMOTRON_1RACK],
    });
    renderAnalytics();
    expect(await screen.findByText("+2 no metrics")).toBeInTheDocument();
  });

  it("says so when a node count is unknown, and when an older server sends no NCCL scaling", async () => {
    respondWith({
      nccl_series_by_log: [],
      nccl_scaling: [{
        num_nodes: null, n_logs: 1, n_logs_no_metrics: 0, logs_no_metrics: [],
        mean_all_reduce_gbps: 100, mean_all_gather_gbps: null, mean_alltoall_gbps: null,
        mean_reduce_scatter_gbps: null, mean_sendrecv_gbps: null,
      }],
      nemotron_scaling: [NEMOTRON_1RACK],
    });
    const first = renderAnalytics();
    const table = await screen.findByRole("table", { name: "NCCL scaling" });
    expect(within(within(table).getAllByRole("row")[1]).getAllByRole("cell")[0]).toHaveTextContent("unknown");
    first.unmount();

    respondWith({ nccl_series_by_log: [], nemotron_scaling: [NEMOTRON_1RACK] });
    renderAnalytics();
    expect(await screen.findByText("No NCCL scaling data yet.")).toBeInTheDocument();
  });

  it("leaves the Nemotron table to NeMo logs: a bucket with only 1-rack data shows no phantom rows", async () => {
    respondWith({ nccl_series_by_log: [], nccl_scaling: [], nemotron_scaling: [NEMOTRON_1RACK] });
    renderAnalytics();
    await screen.findByRole("table", { name: "NCCL scaling" });
    expect(screen.queryByText(/^\+\d+ no metrics$/)).not.toBeInTheDocument();
    expect(screen.getByText(/an NCCL log never does/)).toBeInTheDocument();
  });

  it("falls back to nodes x the platform's GPUs per node, marked as a design figure", async () => {
    platform.current = { geometry: { trays_per_rack: 18, gpus_per_node: 4 } };
    respondWith({
      nccl_series_by_log: [],
      nccl_scaling: [{
        num_nodes: 32, num_gpus: null, n_logs: 22, n_logs_no_metrics: 0, logs_no_metrics: [],
        mean_all_reduce_gbps: null, mean_all_gather_gbps: null, mean_alltoall_gbps: 122.7,
        mean_reduce_scatter_gbps: null, mean_sendrecv_gbps: null,
      }],
      nemotron_scaling: [NEMOTRON_1RACK],
    });
    renderAnalytics();
    const table = await screen.findByRole("table", { name: "NCCL scaling" });
    const gpus = within(within(table).getAllByRole("row")[1]).getAllByRole("cell")[1];
    expect(gpus).toHaveTextContent("≈128");
    expect(gpus).toHaveAttribute("title", expect.stringContaining("platform design"));
  });
});

describe("AnalyticsView single-rack collective switch", () => {
  const block = (rack: string, bw: number) => ({
    sizes_bytes: [1 << 20, 1 << 30],
    median_bus_bw_gbps: [10, bw],
    per_rack: [{ rack, log_file: `${rack}.txt`, bus_bw_gbps: [10, bw], pct_vs_median: [0, 0] }],
  });
  const dev = (metric: string, rack: string, value: number) => ({
    metric, rack, log_file: `${rack}.txt`, value_gbps: value, fleet_median_gbps: 1, pct_vs_median: 0,
  });
  const singleRack = (withPerCollective: boolean) => ({
    nccl_collectives: [],
    nccl_collectives_deviation_pct: [
      dev("all_reduce_gbps", "U20", 909.5),
      dev("all_gather_gbps", "U20", 647.4),
      dev("alltoall_gbps", "U20", 690.7),
    ],
    nccl_ar_series: block("U20", 909.5),
    ...(withPerCollective
      ? { nccl_series_by_collective: { all_reduce: block("U20", 909.5), alltoall: block("U20", 690.7) } }
      : {}),
    nemotron: [],
  });
  const chooseCollective = (label: string) => {
    const control = screen.getByRole("combobox", { name: "Collective" });
    fireEvent.click(control);
    const listboxId = control.getAttribute("aria-controls");
    const option = screen.getAllByRole("option", { hidden: true })
      .find((el) => el.closest(`#${listboxId}`) !== null && el.textContent === label);
    if (!option) throw new Error(`missing option ${label}`);
    fireEvent.click(option);
  };

  it("defaults to all_reduce and shows only that collective's rows", async () => {
    respondWith({ nccl_series_by_log: [], nemotron_scaling: [] }, singleRack(true));
    renderAnalytics();
    expect(await screen.findByText("909.5")).toBeInTheDocument();
    expect(screen.queryByText("690.7")).not.toBeInTheDocument();
    expect(screen.getByText(/all_reduce bus BW vs message size/)).toBeInTheDocument();
  });

  it("switches the table and the chart heading to the chosen collective", async () => {
    respondWith({ nccl_series_by_log: [], nemotron_scaling: [] }, singleRack(true));
    renderAnalytics();
    await screen.findByText("909.5");
    chooseCollective("alltoall_gbps");
    expect(await screen.findByText("690.7")).toBeInTheDocument();
    expect(screen.queryByText("909.5")).not.toBeInTheDocument();
    expect(screen.getByText(/alltoall bus BW vs message size/)).toBeInTheDocument();
    expect(screen.queryByText("No alltoall series data.")).not.toBeInTheDocument();
  });

  it("says so when a collective has no series, as an older server sends only all_reduce", async () => {
    respondWith({ nccl_series_by_log: [], nemotron_scaling: [] }, singleRack(false));
    renderAnalytics();
    await screen.findByText("909.5");
    chooseCollective("all_gather_gbps");
    expect(await screen.findByText("647.4")).toBeInTheDocument();
    expect(screen.getByText("No all_gather series data.")).toBeInTheDocument();
  });
});

describe("AnalyticsView single-rack table sorting", () => {
  const dev = (rack: string, value: number, pct: number) => ({
    metric: "all_reduce_gbps", rack, log_file: `${rack}.txt`, value_gbps: value, fleet_median_gbps: 1, pct_vs_median: pct,
  });
  const nemo = (rack: string, tflops: number | null, pct: number | null) => ({
    rack, log_file: `${rack}-nemo.txt`, median_step_time: 4.1, tflops_per_gpu: tflops,
    step_time_cv_pct: 1, pct_vs_mean_tflops: pct, pct_vs_mean_step_time: 0,
  });
  const singleRack = {
    nccl_collectives: [],
    nccl_collectives_deviation_pct: [dev("U10", 905, -0.5), dev("U2", 950, 4.4), dev("U9", 870, -4.3)],
    nccl_ar_series: { sizes_bytes: [], median_bus_bw_gbps: [], per_rack: [] },
    nemotron: [nemo("Q1", 1990, 0.3), nemo("Q2", null, null), nemo("Q10", 1900, -4.2)],
  };
  /** The rack column of a table, top to bottom. */
  const racksIn = (table: HTMLElement) =>
    within(table).getAllByRole("row").slice(1).map((row) => within(row).getAllByRole("cell")[0].textContent);
  const tables = async () => {
    await screen.findByText("U10.txt");
    const [deviation, nemotron] = screen.getAllByRole("table").filter((t) => within(t).queryByText(/Log/));
    return { deviation, nemotron };
  };
  // Racks are the 2nd deviation column; the nemotron table's first column is the rack.
  const devRacks = (t: HTMLElement) =>
    within(t).getAllByRole("row").slice(1).map((row) => within(row).getAllByRole("cell")[1].textContent);

  it("sorts the NCCL rows by value, reverses, then restores the server's order", async () => {
    respondWith({ nccl_series_by_log: [], nemotron_scaling: [] }, singleRack);
    renderAnalytics();
    const { deviation } = await tables();
    expect(devRacks(deviation)).toEqual(["U10", "U2", "U9"]);
    const header = within(deviation).getByRole("button", { name: "Value (GB/s)" });
    fireEvent.click(header);
    expect(devRacks(deviation)).toEqual(["U9", "U10", "U2"]);
    fireEvent.click(header);
    expect(devRacks(deviation)).toEqual(["U2", "U10", "U9"]);
    fireEvent.click(header);
    expect(devRacks(deviation)).toEqual(["U10", "U2", "U9"]);
  });

  it("orders racks naturally and signed percentages numerically", async () => {
    respondWith({ nccl_series_by_log: [], nemotron_scaling: [] }, singleRack);
    renderAnalytics();
    const { deviation } = await tables();
    fireEvent.click(within(deviation).getByRole("button", { name: "Rack" }));
    expect(devRacks(deviation)).toEqual(["U2", "U9", "U10"]);
    fireEvent.click(within(deviation).getByRole("button", { name: "% vs median" }));
    expect(devRacks(deviation)).toEqual(["U9", "U10", "U2"]);
  });

  it("sorts the Nemotron table with a missing value last in both directions", async () => {
    respondWith({ nccl_series_by_log: [], nemotron_scaling: [] }, singleRack);
    renderAnalytics();
    const { nemotron } = await tables();
    const header = within(nemotron).getByRole("button", { name: "TFLOPs/GPU" });
    fireEvent.click(header);
    expect(racksIn(nemotron)).toEqual(["Q10", "Q1", "Q2"]);
    fireEvent.click(header);
    expect(racksIn(nemotron)).toEqual(["Q1", "Q10", "Q2"]);
  });
});
