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
// Rollouts: what an operator can do to a live pull rollout -- open it, retry one node or every
// deferred node, change the pacing, cancel only after confirming -- and what they see when the
// cluster is not configured for it or P2P is off.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../lib/apiFetch";
import { getCatalog } from "../services/fleetService";
import {
  cancelRollout, getRollout, listRollouts, planRollout, retryRollout, setRolloutPacing, startRollout,
} from "../services/rolloutService";
import type { RolloutDetail, RolloutsResponse } from "../types/rollout";
import RolloutsView from "./RolloutsView";

vi.mock("../services/fleetService", () => ({ getCatalog: vi.fn() }));
vi.mock("../services/rolloutService", () => ({
  listRollouts: vi.fn(), getRollout: vi.fn(), planRollout: vi.fn(), startRollout: vi.fn(),
  cancelRollout: vi.fn(), retryRollout: vi.fn(), setRolloutPacing: vi.fn(),
}));
vi.mock("../../../context/ActiveClusterContext", () => ({
  useActiveCluster: () => ({ activeCluster: { name: "Nscale NC" }, version: 0 }),
}));

const fn = (f: unknown) => f as ReturnType<typeof vi.fn>;
const IMAGE = "nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh";
const PACING = { stages: ["1", "50%", "100%"], budget_seconds: 3600, max_active_racks: 4 };
const counts = { total: 3, pending: 0, released: 0, downloading: 1, ready: 1, deferred: 1 };
const row = {
  run_id: "r-1", state: "running", started_at: "2026-10-10T03:19:01Z", updated_at: "", operator: "rkoike",
  cluster_id: "nc", image: IMAGE, racks: ["Q5"], pacing: PACING, counts, last_error: "",
};
const detail: RolloutDetail = {
  ...row,
  rack_detail: [{
    rack: "Q5", state: "active", rung: 1, cuts: [1, 2, 3], nodes: [
      { node: "gpu55-su22", state: "ready", reason: "", rung: 0, released_at: 1, ready_at: 2, attempts: 1 },
      { node: "gpu56-su22", state: "deferred", reason: "failed_3x", rung: 1, released_at: 1, ready_at: 0, attempts: 1 },
      { node: "gpu57-su22", state: "downloading", reason: "", rung: 1, released_at: 1, ready_at: 0, attempts: 1 },
    ],
  }],
  log: ["03:19:01 release gpu55-su22 rack=Q5 rung=0 generation=1"],
  image_pin: { filename: IMAGE, sha256: "2acf607c".padEnd(64, "0"), bytes: 42488647680 },
};
const list = (over: Partial<RolloutsResponse> = {}): RolloutsResponse => ({
  ok: true, rollouts: [row], racks: [{ rack: "Q5", nodes: 18 }], blockers: [], defaults: PACING, ...over,
});

beforeEach(() => {
  fn(getCatalog).mockResolvedValue({ ok: true, images: [{ filename: IMAGE, sha256: "a".repeat(64) }] });
  fn(listRollouts).mockResolvedValue(list());
  fn(getRollout).mockResolvedValue(detail);
  fn(retryRollout).mockResolvedValue(row);
  fn(cancelRollout).mockResolvedValue({ ok: true, state: "cancelled" });
  fn(setRolloutPacing).mockResolvedValue(row);
  fn(planRollout).mockResolvedValue(null);
  fn(startRollout).mockResolvedValue({ ok: true, run_id: "r-2" });
});
afterEach(() => vi.clearAllMocks());

async function openRollout() {
  render(<RolloutsView />);
  const rollouts = await screen.findByRole("table", { name: "Rollouts" });
  expect(within(rollouts).getByText("1/3 ready · 1 in flight · 1 deferred")).toBeTruthy();
  fireEvent.click(within(rollouts).getByRole("button", { name: "Open" }));
  return screen.findByTestId("rollout-detail");
}

describe("RolloutsView", () => {
  it("opens a rollout with each rack's rungs and nodes", async () => {
    const card = await openRollout();
    expect(within(card).getByText("rung 2 of 3 (targets 1, 2, 3)")).toBeTruthy();
    expect(within(card).getByText("failed_3x")).toBeTruthy();
    expect(within(card).getByText(/release gpu55-su22/)).toBeTruthy();
    expect(getRollout).toHaveBeenCalledWith("r-1");
  });

  it("retries one deferred node, or all of them", async () => {
    const card = await openRollout();
    const nodes = within(card).getByRole("table", { name: "Rack Q5 nodes" });
    fireEvent.click(within(nodes).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(retryRollout).toHaveBeenCalledWith("r-1", ["gpu56-su22"]));
    fireEvent.click(within(card).getByRole("button", { name: "Retry deferred" }));
    await waitFor(() => expect(retryRollout).toHaveBeenCalledWith("r-1", []));
  });

  it("cancels only after confirming", async () => {
    const card = await openRollout();
    fireEvent.click(within(card).getByRole("button", { name: "Cancel" }));
    expect(cancelRollout).not.toHaveBeenCalled();
    fireEvent.click(within(card).getByRole("button", { name: "Confirm cancel" }));
    await waitFor(() => expect(cancelRollout).toHaveBeenCalledWith("r-1"));
  });

  it("applies new pacing to the running rollout", async () => {
    const card = await openRollout();
    const racks = within(card).getAllByRole("textbox").find((el) => (el as HTMLInputElement).value === "4")!;
    fireEvent.change(racks, { target: { value: "2" } });
    fireEvent.click(within(card).getByRole("button", { name: "Apply pacing" }));
    await waitFor(() => expect(setRolloutPacing).toHaveBeenCalledWith("r-1", { ...PACING, max_active_racks: 2 }));
  });

  it("offers no controls on a finished rollout", async () => {
    fn(getRollout).mockResolvedValue({ ...detail, state: "completed" });
    const card = await openRollout();
    expect(within(card).queryByRole("button", { name: "Cancel" })).toBeNull();
    expect(within(card).queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("says what to configure, and will not start, before rollouts are set up", async () => {
    fn(listRollouts).mockResolvedValue(list({ rollouts: [], blockers: ["desired_dir is not set"] }));
    render(<RolloutsView />);
    expect(await screen.findByText(/set in Dragonfly Settings: desired_dir is not set/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Start rollout" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Preview" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("explains a cluster without P2P", async () => {
    fn(listRollouts).mockRejectedValue(new ApiError(404, "P2P is not enabled", null));
    render(<RolloutsView />);
    expect(await screen.findByText(/P2P is not enabled for this cluster/)).toBeTruthy();
  });
});
