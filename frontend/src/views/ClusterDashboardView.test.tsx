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
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ClusterDashboardView } from "./ClusterDashboardView";
import type {
  ClusterOverviewLiveResponse,
  ClusterOverviewResponse,
  ClusterSnapshotMeta,
  ClusterSqueueBlock,
  LiveProbeSection,
  LiveProbeState,
} from "../types/api";

const snapshot = (
  state: ClusterSnapshotMeta["state"],
  overrides: Partial<ClusterSnapshotMeta> = {}
): ClusterSnapshotMeta => ({
  id: state === "initializing" || state === "failed" ? null : "snap-1",
  state,
  cluster_id: "cluster-a",
  log_root: "/logs",
  configuration_generation: 1,
  last_attempt_at: "2026-07-22T15:00:00Z",
  last_success_at: state === "initializing" || state === "failed" ? null : "2026-07-22T14:59:00Z",
  age_seconds: state === "initializing" || state === "failed" ? null : 60,
  last_error: state === "stale" || state === "failed" ? "inventory failed" : null,
  ...overrides,
});

const overview = (
  state: ClusterSnapshotMeta["state"],
  racks: number | null,
  nodes: number | null,
  overrides: Partial<ClusterOverviewResponse> = {}
): ClusterOverviewResponse => ({
  ok: true,
  snapshot: snapshot(state),
  cluster_name: "boston",
  customer_name: "Acme",
  product_line: "NVL",
  hw_vendor: "NVIDIA",
  bcm_version: "1",
  slurm_version: "2",
  nemo_container: "nemo.sqsh",
  rack_count: racks,
  tray_hosts: nodes,
  estimated_gpu_count: nodes === null ? null : nodes * 8,
  validation_summary:
    racks === null ? null : { done: racks, fail: 0, ongoing: 0, pending: 0 },
  validation_cells_total: racks,
  status_updated_at: racks === null ? null : "2026-07-22T14:59:00Z",
  probes: {},
  app_version: "1",
  connected_users: 1,
  squeue: { ok: true, stdout: "" },
  cluster_dashboard_metadata: { product_line: "", hw_vendor: "", customer_name: "" },
  ...overrides,
});

describe("ClusterDashboardView snapshot states", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        json: async () => ({ ok: false }),
      })
    );
  });

  it("shows cluster identity and page shell while inventory is initializing", () => {
    render(
      <ClusterDashboardView
        data={overview("initializing", null, null)}
        loading={false}
        error={null}
      />
    );
    expect(screen.getByText("Overview")).toBeInTheDocument();
    expect(screen.getByText("boston")).toBeInTheDocument();
    expect(screen.getByText("Acme")).toBeInTheDocument();
    expect(screen.getByText("Loading cluster inventory…")).toBeInTheDocument();
  });

  it("shows loading inventory placeholders without false-zero counts", () => {
    render(
      <ClusterDashboardView
        data={overview("initializing", null, null)}
        loading={false}
        error={null}
      />
    );
    expect(screen.getByText("Loading cluster inventory…")).toBeInTheDocument();
    expect(screen.getByText("Racks")).toBeInTheDocument();
    expect(screen.getByText("Nodes")).toBeInTheDocument();
    // Null inventory must not render as literal zeros.
    expect(screen.queryByText(/^0$/)).not.toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(2);
    expect(document.querySelectorAll("[class*='nv-skeleton']").length).toBeGreaterThan(0);
  });

  it("shows unavailable error with neutral em dashes and no loading skeletons", () => {
    const retry = vi.fn();
    render(
      <ClusterDashboardView
        data={overview("failed", null, null)}
        loading={false}
        error={null}
        onRetry={retry}
      />
    );
    expect(screen.getByText("Cluster inventory unavailable")).toBeInTheDocument();
    expect(screen.getByText(/inventory failed/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry inventory" }));
    expect(retry).toHaveBeenCalledOnce();
    expect(screen.getByText("Racks")).toBeInTheDocument();
    expect(screen.getByText("Nodes")).toBeInTheDocument();
    expect(screen.getByText("Done")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(5);
    expect(document.querySelectorAll("[class*='nv-skeleton']").length).toBe(0);
    expect(screen.queryByText(/will appear when/i)).not.toBeInTheDocument();
  });

  it("shows stale warning with last-good non-empty counts", () => {
    render(
      <ClusterDashboardView data={overview("stale", 1, 16)} loading={false} error={null} />
    );
    expect(screen.getByText(/Showing stale cluster inventory/)).toBeInTheDocument();
    expect(screen.getByText("Racks")).toBeInTheDocument();
    expect(screen.getByText("Nodes")).toBeInTheDocument();
    expect(screen.getByText("16")).toBeInTheDocument();
  });

  const liveSection = <T,>(
    state: LiveProbeState,
    value: T | null = null,
    lastError: string | null = null
  ): LiveProbeSection<T> => ({
    state,
    value,
    last_attempt_at: null,
    last_success_at: state === "fresh" || state === "stale" ? "2026-07-22T15:00:00Z" : null,
    age_seconds: state === "fresh" || state === "stale" ? 30 : null,
    last_error: lastError,
  });

  const liveResponse = (
    overrides: Partial<ClusterOverviewLiveResponse["sections"]> = {}
  ): ClusterOverviewLiveResponse => ({
    ok: true,
    cluster_id: "cluster-a",
    reachable: true,
    unreachable_detail: "",
    sections: {
      bcm: liveSection<string>("fresh", "BCM 11.0"),
      slurm: liveSection<string>("fresh", "slurm 25.05"),
      queue: liveSection<ClusterSqueueBlock>("fresh", { ok: true, stdout: "JOBID\n1" }),
      ...overrides,
    },
  });

  it("renders the page shell while base overview is still loading", () => {
    render(
      <ClusterDashboardView
        data={null}
        loading
        error={null}
        liveData={null}
        liveLoading
        liveError={null}
      />
    );
    expect(screen.getByText("Overview")).toBeInTheDocument();
    expect(screen.queryByTestId("full-page-loading-skeleton")).not.toBeInTheDocument();
  });

  it("keeps inventory visible while live BCM and scheduler details initialize", () => {
    render(
      <ClusterDashboardView
        data={overview("fresh", 2, 32, {
          bcm_version: "—",
          slurm_version: "—",
          squeue: { ok: false, stdout: "", error: "live details load separately" },
        })}
        loading={false}
        error={null}
        liveData={liveResponse({
          bcm: liveSection<string>("initializing"),
          slurm: liveSection<string>("initializing"),
          queue: liveSection<ClusterSqueueBlock>("initializing"),
        })}
        liveLoading
        liveError={null}
      />
    );
    expect(screen.getByText("boston")).toBeInTheDocument();
    expect(screen.getAllByText("2").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("32")).toBeInTheDocument();
    expect(screen.getByText("BCM version")).toBeInTheDocument();
    expect(screen.getByText("Scheduler version")).toBeInTheDocument();
    expect(document.querySelectorAll("[class*='nv-skeleton']").length).toBeGreaterThan(0);
    expect(screen.queryByText("live details load separately")).not.toBeInTheDocument();
  });

  it("shows stale live values with a warning banner", () => {
    render(
      <ClusterDashboardView
        data={overview("fresh", 1, 16, { bcm_version: "—", slurm_version: "—" })}
        loading={false}
        error={null}
        liveData={liveResponse({
          bcm: liveSection<string>("stale", "BCM 10.9", "probe timed out"),
          slurm: liveSection<string>("fresh", "slurm 25.05"),
          queue: liveSection<ClusterSqueueBlock>("fresh", { ok: true, stdout: "JOBID\n2" }),
        })}
        liveLoading={false}
        liveError={null}
      />
    );
    expect(screen.getByText(/live cluster details may be stale/i)).toBeInTheDocument();
    expect(screen.getByText("BCM 10.9")).toBeInTheDocument();
    expect(screen.getAllByText("1").length).toBeGreaterThanOrEqual(1);
  });

  it("shows live-details retry without hiding inventory", () => {
    const liveRetry = vi.fn();
    render(
      <ClusterDashboardView
        data={overview("fresh", 1, 16, {
          bcm_version: "—",
          slurm_version: "—",
          squeue: { ok: false, stdout: "", error: "live details load separately" },
        })}
        loading={false}
        error={null}
        liveData={liveResponse({
          bcm: liveSection<string>("failed", null, "cmsh unavailable"),
          slurm: liveSection<string>("fresh", "slurm 25.05"),
          queue: liveSection<ClusterSqueueBlock>("failed", null, "squeue failed"),
        })}
        liveLoading={false}
        liveError="live probe gateway down"
        onLiveRetry={liveRetry}
      />
    );
    expect(screen.getAllByText("1").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/live cluster details unavailable/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry live details" }));
    expect(liveRetry).toHaveBeenCalledOnce();
  });

  it.each([
    ["fresh non-empty", 1, 16],
    ["fresh confirmed empty", 0, 0],
  ])("renders %s inventory normally", (_label, racks, nodes) => {
    render(
      <ClusterDashboardView data={overview("fresh", racks, nodes)} loading={false} error={null} />
    );
    expect(screen.getByText("Racks")).toBeInTheDocument();
    expect(screen.getByText("Nodes")).toBeInTheDocument();
    if (nodes > 0) {
      expect(screen.getByText(String(nodes))).toBeInTheDocument();
    }
    expect(screen.queryByText("Loading cluster inventory…")).not.toBeInTheDocument();
  });
});
