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
import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { AnalyticsView } from "./AnalyticsView";

// The multirack section is under test, not the charts: canvas rendering needs a real browser.
vi.mock("react-chartjs-2", () => ({ Line: () => null, Bar: () => null }));
vi.mock("../context/ActiveClusterContext", () => ({
  useActiveCluster: () => ({ activeCluster: null, activePlatform: { geometry: { trays_per_rack: 18 } } }),
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

function respondWith(multirack: Record<string, unknown>) {
  apiFetch.mockImplementation(async (url: string) => {
    if (String(url).startsWith("/api/analytics/last")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          ok: true,
          cached: true,
          analyzed_at: "2026-10-12T00:00:00Z",
          single_rack: null,
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

beforeEach(() => apiFetch.mockReset());

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
          num_nodes: 36, n_logs: 1, n_logs_no_metrics: 0, logs_no_metrics: [],
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
      ["36", "2", "1", "798", "—", "135", "—", "—"],
    );
    expect(within(rows[0]).getAllByRole("cell")[1]).toHaveTextContent("1");
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
});
