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
// Campaigns: a launch puts an image on every node of a roster, so it must never happen on one click.
// These pin the confirm-before-launch flow, what an operator sees for each outcome (launched, already
// running, P2P off), that an incomplete row cannot launch, and that the editor draws its choices
// from the catalog and rosters it loaded.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../lib/apiFetch";
import { getCampaigns, getCatalog, getRosters, launchCampaign, saveCampaigns } from "../services/fleetService";
import type { CampaignEdit } from "../services/fleetService";
import CampaignsView from "./CampaignsView";

vi.mock("../services/fleetService", () => ({
  getCampaigns: vi.fn(),
  getCatalog: vi.fn(),
  getRosters: vi.fn(),
  saveCampaigns: vi.fn(),
  launchCampaign: vi.fn(),
}));
vi.mock("../../../context/ActiveClusterContext", () => ({
  useActiveCluster: () => ({ activeCluster: { name: "Nscale NC" }, version: 0 }),
}));
// The dialog fetches the log itself; here only "which operation did the view open" matters.
vi.mock("../components/OperationLogDialog", () => ({
  OperationLogDialog: ({ open, operation }: { open: boolean; operation: { id: string } | null }) =>
    open && operation ? <div role="dialog">log of {operation.id}</div> : null,
}));

const m = {
  getCampaigns: getCampaigns as unknown as ReturnType<typeof vi.fn>,
  getCatalog: getCatalog as unknown as ReturnType<typeof vi.fn>,
  getRosters: getRosters as unknown as ReturnType<typeof vi.fn>,
  saveCampaigns: saveCampaigns as unknown as ReturnType<typeof vi.fn>,
  launchCampaign: launchCampaign as unknown as ReturnType<typeof vi.fn>,
};

const NCCL = "nvidia+nemo+26.04.01-arm-nccl2.32.3.sqsh";
const campaign: CampaignEdit = {
  name: "nemo-nccl2323-fleet",
  description: "",
  image: NCCL,
  roster: "All fleet",
  wave_size: 50,
  parallelism: 0,
  poll_seconds: 0,
  max_launches: 0,
  max_unreachable: 0,
  timeout_seconds: 0,
  fill_wave_size: 0,
  stall_seconds: 0,
  verify_mb_per_second: 0,
  download_timeout_seconds: 0,
  strategy: "",
  stages: [],
};
const op = { id: "distribute-20261007T155927Z-1310d8", action: "distribute", sus: [], images: [NCCL], state: "running" };

function catalogOf(...filenames: string[]) {
  return {
    ok: true,
    cluster_id: "default",
    authored: true,
    images: filenames.map((filename, i) => ({ order: i + 1, name: filename, filename, bytes: 1, sha256: "", required_by: [] })),
  };
}

beforeEach(() => {
  m.getCampaigns.mockResolvedValue({ ok: true, cluster_id: "default", campaigns: [campaign] });
  m.getCatalog.mockResolvedValue(catalogOf(NCCL, "nvbandwidth-v0.9-arm.sqsh"));
  m.getRosters.mockResolvedValue({ ok: true, cluster_id: "default", rosters: [{ name: "All fleet", description: "", nodes: ["n1"] }] });
});

afterEach(() => {
  vi.clearAllMocks();
});

async function renderLoaded() {
  render(<CampaignsView />);
  await screen.findByText(/nemo-nccl2323-fleet · nvidia\+nemo/);
}

describe("CampaignsView", () => {
  it("lists each campaign with its image and target, and offers the catalog's images and the rosters", async () => {
    await renderLoaded();
    expect(screen.getByText(`nemo-nccl2323-fleet · ${NCCL} → All fleet`)).toBeInTheDocument();
    const imageOptions = within(screen.getByLabelText("Image")).getAllByRole("option").map((o) => o.textContent);
    expect(imageOptions).toEqual(["— choose an image —", NCCL, "nvbandwidth-v0.9-arm.sqsh"]);
    const rosterOptions = within(screen.getByLabelText("Roster")).getAllByRole("option").map((o) => o.textContent);
    expect(rosterOptions).toEqual(["— whole fleet (default inventory) —", "All fleet"]);
  });

  it("launches only after an explicit confirm, then offers the launched operation's log", async () => {
    m.launchCampaign.mockResolvedValue(op);
    await renderLoaded();

    fireEvent.click(screen.getByRole("button", { name: "Launch" }));
    expect(m.launchCampaign).not.toHaveBeenCalled();
    expect(screen.getByText(/distributes nvidia\+nemo.*to\s*All fleet/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Confirm launch" }));
    await screen.findByText(`Launched "nemo-nccl2323-fleet" — operation ${op.id} is running.`);
    expect(m.launchCampaign).toHaveBeenCalledWith(campaign);

    fireEvent.click(screen.getByRole("button", { name: "View log" }));
    expect(screen.getByRole("dialog")).toHaveTextContent(`log of ${op.id}`);
  });

  it("cancelling the confirm launches nothing", async () => {
    await renderLoaded();
    fireEvent.click(screen.getByRole("button", { name: "Launch" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("button", { name: "Confirm launch" })).not.toBeInTheDocument();
    expect(m.launchCampaign).not.toHaveBeenCalled();
  });

  it("explains a refused launch when another operation is already running", async () => {
    m.launchCampaign.mockRejectedValue(new ApiError(409, "conflict", null));
    await renderLoaded();
    fireEvent.click(screen.getByRole("button", { name: "Launch" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm launch" }));
    await screen.findByText(/Another operation is already running on this cluster/);
    expect(screen.queryByRole("button", { name: "View log" })).not.toBeInTheDocument();
  });

  it("says P2P is off for this cluster instead of failing on a 404", async () => {
    m.getCampaigns.mockRejectedValue(new ApiError(404, "not enabled", null));
    render(<CampaignsView />);
    await screen.findByText(/P2P is not enabled for this cluster/);
  });

  it("cannot launch a row that has no name or image yet", async () => {
    await renderLoaded();
    fireEvent.click(screen.getByRole("button", { name: "Add campaign" }));
    const launches = screen.getAllByRole("button", { name: "Launch" });
    expect(launches).toHaveLength(2);
    expect(launches[0]).toBeEnabled();
    expect(launches[1]).toBeDisabled();
  });

  it("warns that there is nothing to distribute when the catalog is empty", async () => {
    m.getCatalog.mockResolvedValue(catalogOf());
    await renderLoaded();
    expect(screen.getByText(/The catalog is empty/)).toBeInTheDocument();
  });

  it("hides the ladder until the campaign stages by rack, and saves the plan it is given", async () => {
    m.saveCampaigns.mockImplementation(async (campaigns: CampaignEdit[]) => ({ ok: true, cluster_id: "default", campaigns }));
    await renderLoaded();
    expect(screen.getByRole("radio", { name: "Cluster default" })).toBeChecked();
    expect(screen.queryByLabelText("Stages")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: "Rack-staged" }));
    const stages = screen.getByLabelText("Stages");
    // Blank inherits the cluster's ladder, so it is not an error here.
    expect(screen.queryByText(/At least one stage/)).not.toBeInTheDocument();
    fireEvent.change(stages, { target: { value: "1, 2, 4" } });
    expect(screen.getByText(/last stage must be 100%/)).toBeInTheDocument();
    fireEvent.change(stages, { target: { value: "1, 2, 4, 8, 100%" } });
    expect(screen.queryByText(/last stage must be 100%/)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Fill wave size"), { target: { value: "72" } });

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(m.saveCampaigns).toHaveBeenCalledTimes(1));
    expect(m.saveCampaigns.mock.calls[0][0]).toEqual([
      { ...campaign, strategy: "rack-staged", stages: ["1", "2", "4", "8", "100%"], fill_wave_size: 72 },
    ]);
  });

  it("going back to the cluster default clears the strategy override", async () => {
    m.getCampaigns.mockResolvedValue({ ok: true, cluster_id: "default",
      campaigns: [{ ...campaign, strategy: "rack-staged", stages: ["1", "50%", "100%"] }] });
    m.saveCampaigns.mockImplementation(async (campaigns: CampaignEdit[]) => ({ ok: true, cluster_id: "default", campaigns }));
    await renderLoaded();
    expect(screen.getByLabelText("Stages")).toHaveValue("1, 50%, 100%");
    fireEvent.click(screen.getByRole("radio", { name: "Cluster default" }));
    expect(screen.queryByLabelText("Stages")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(m.saveCampaigns).toHaveBeenCalledTimes(1));
    expect(m.saveCampaigns.mock.calls[0][0][0].strategy).toBe("");
  });

  it("saves a large-image verify rate override", async () => {
    m.saveCampaigns.mockImplementation(async (campaigns: CampaignEdit[]) => ({ ok: true, cluster_id: "default", campaigns }));
    await renderLoaded();
    fireEvent.change(screen.getByLabelText("Verify rate (MB/s)"), { target: { value: "50" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(m.saveCampaigns).toHaveBeenCalledTimes(1));
    expect(m.saveCampaigns.mock.calls[0][0]).toEqual([{ ...campaign, verify_mb_per_second: 50 }]);
  });

  it("saves the edited campaigns, with a pacing field never below zero", async () => {
    m.saveCampaigns.mockImplementation(async (campaigns: CampaignEdit[]) => ({ ok: true, cluster_id: "default", campaigns }));
    await renderLoaded();
    const [waveSize] = screen.getAllByRole("spinbutton");
    fireEvent.change(waveSize, { target: { value: "-5" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(m.saveCampaigns).toHaveBeenCalledTimes(1));
    expect(m.saveCampaigns.mock.calls[0][0]).toEqual([{ ...campaign, wave_size: 0 }]);
    await screen.findByText("Campaigns saved.");
  });
});
