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

/**
 * `ProjectsView` — the rack mapping mode toggle.
 *
 * "Direct" clusters embed the rack id in the hostname, so the node/CT template parses it.
 * "Translate" clusters (Nscale NC's `sys1-gso1-p-phy-gpu1-su19`) do not — placement comes from
 * the provider and the SU grouping from an explicit list. The form has to switch between those
 * two shapes: the CT/NVSwitch templates are meaningless in translate mode, and the SU
 * translation list is meaningless in direct mode. This asserts each control appears only in the
 * mode where it means something.
 *
 * `fireEvent` and the `choose` combobox idiom match ToolRegistryView.test.tsx.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as catalogService from "../services/catalogService";
import { ActiveClusterContext, type ActiveClusterState } from "../context/ActiveClusterContext";
import type { CatalogVocab, Project } from "../types/catalog";
import { ProjectsView } from "./ProjectsView";

vi.mock("../services/catalogService");

const VOCAB = {
  ok: true,
  gpu_models: [],
  cpu_vendors: [],
  system_vendors: [],
  form_factors: [],
  cluster_managers: ["bcm", "maas"],
  schedulers: ["slurm", "pythoncm"],
  tool_categories: [],
  exec_modes: [],
  exec_targets: [],
  statuses: ["ga"],
} as unknown as CatalogVocab;

const PROJECT = {
  id: "p1",
  name: "Project One",
  customer: "Acme",
  description: "",
  clusters: [],
} as unknown as Project;

const clusterStub = (): ActiveClusterState => ({
  projects: [PROJECT],
  active: { project: "", cluster: "" },
  activeProject: null,
  activeCluster: null,
  activePlatform: null,
  aiContext: "",
  loading: false,
  error: null,
  version: 0,
  refresh: async () => {},
  setActive: async () => {},
  catalogChanged: vi.fn(),
});

function renderView() {
  return render(
    <ActiveClusterContext.Provider value={clusterStub()}>
      <ProjectsView />
    </ActiveClusterContext.Provider>,
  );
}

/** Open the New-cluster modal for the seeded project. */
async function openNewCluster() {
  vi.mocked(catalogService.getVocab).mockResolvedValue(VOCAB);
  vi.mocked(catalogService.listPlatforms).mockResolvedValue([]);
  renderView();
  fireEvent.click(await screen.findByRole("button", { name: /^Cluster$/ }));
  await waitFor(() => expect(screen.getByText("New cluster")).toBeTruthy());
}

/** Pick an option in a KUI Select, scoped to the listbox the named control owns. */
function choose(controlLabel: string | RegExp, optionText: RegExp) {
  const control = screen.getByRole("combobox", { name: controlLabel });
  const listboxId = control.getAttribute("aria-controls");
  const options = screen
    .getAllByRole("option", { hidden: true })
    .filter((o) => (listboxId ? o.closest(`#${listboxId}`) !== null : true));
  const match = options.find((o) => optionText.test(o.textContent ?? ""));
  if (!match) {
    throw new Error(
      `option ${String(optionText)} not in ${String(controlLabel)}; saw ` +
        JSON.stringify(options.map((o) => o.textContent)),
    );
  }
  fireEvent.click(match);
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("rack mapping mode", () => {
  it("makes SU grouping optional only for BCM with Slurm", async () => {
    await openNewCluster();
    choose(/Rack mapping/i, /Translate/);
    await waitFor(() => expect(screen.getByText(/Optional for BCM with Slurm; leave empty/)).toBeTruthy());
    choose(/Cluster manager/i, /^maas$/);
    await waitFor(() => expect(screen.getByText(/Required in translate mode\./)).toBeTruthy());
    choose(/Cluster manager/i, /^bcm$/);
    choose(/^Scheduler$/i, /^pythoncm$/);
    await waitFor(() => expect(screen.getByText(/Required in translate mode\./)).toBeTruthy());
  });

  it("defaults to direct: shows CT/NVSwitch templates, hides the SU list", async () => {
    await openNewCluster();
    expect(screen.getByText("Node/CT id template")).toBeTruthy();
    expect(screen.getByText("NVSwitch id template")).toBeTruthy();
    expect(screen.queryByText("Scalable Unit translation")).toBeNull();
  });

  it("switching to translate reveals the SU list and hides the CT/NVSwitch templates", async () => {
    await openNewCluster();
    choose(/Rack mapping/i, /Translate/);
    await waitFor(() => expect(screen.getByText("Scalable Unit translation")).toBeTruthy());
    expect(screen.queryByText("Node/CT id template")).toBeNull();
    expect(screen.queryByText("NVSwitch id template")).toBeNull();
    // The rack id template stays — it still decomposes the provider's rack id into a row.
    expect(screen.getByText("Rack id template")).toBeTruthy();
  });

  it("adds an SU row on demand in translate mode", async () => {
    await openNewCluster();
    choose(/Rack mapping/i, /Translate/);
    await waitFor(() => expect(screen.getByText("Scalable Unit translation")).toBeTruthy());
    expect(screen.queryByText("SU label")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Add SU/ }));
    await waitFor(() => expect(screen.getByText("SU label")).toBeTruthy());
    expect(screen.getByText(/Racks \(comma-separated\)/)).toBeTruthy();
  });
});
