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

describe("compute fabric", () => {
  /** The saved project payload from the last saveProject call. */
  const savedProject = () => vi.mocked(catalogService.saveProject).mock.calls.at(-1)?.[0] as Project;

  it("states the fabric overrides on a new cluster and saves them", async () => {
    await openNewCluster();
    fireEvent.change(screen.getByPlaceholderText("e.g. Pod A"), { target: { value: "NC" } });
    choose(/RoCE address family/i, /IPv6/);
    fireEvent.change(screen.getByPlaceholderText("Detect (e.g. roce_vf_r)"), { target: { value: "roce_vf_r" } });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(catalogService.saveProject).toHaveBeenCalled());
    expect(savedProject().clusters[0].fabric).toEqual({ scaleout: "", addr_family: "ipv6", compute_hca: "roce_vf_r" });
  });

  it("opens a cluster stored before the field existed, and saves it as all-detect", async () => {
    // The running backend omits `fabric` until it is rebuilt; the editor must not crash on that.
    const legacy = {
      id: "nc",
      name: "NC",
      platform_id: "",
      cluster_manager: "bcm",
      scheduler: "slurm",
      rack_mapping_mode: "translate",
      site_prefix: "",
      naming: { rack_id_template: "", ct_id_template: "", nvsw_id_template: "", ct_joiner: "-ct", slurm_hostlist: "" },
      geometry: { pods: 1, rows_per_pod: 0, racks_per_row: 0, trays_per_rack: 0 },
      connection: {
        exec_mode: "", ssh_host: "", ssh_user: "", ssh_key: "", remote_base_path: "",
        log_dir: "", workspace_dir: "", shared_container_dir: "", node_container_dir: "",
      },
      scalable_units: [],
      campaigns: [],
      evidence: { snapshot_path: "", healthcheck_root: "" },
      p2p_enabled: false,
    };
    const project = { ...PROJECT, clusters: [legacy] } as unknown as Project;
    vi.mocked(catalogService.getVocab).mockResolvedValue(VOCAB);
    vi.mocked(catalogService.listPlatforms).mockResolvedValue([]);
    render(
      <ActiveClusterContext.Provider value={{ ...clusterStub(), projects: [project] }}>
        <ProjectsView />
      </ActiveClusterContext.Provider>,
    );
    const edits = await screen.findAllByRole("button", { name: /^Edit$/ });
    fireEvent.click(edits[edits.length - 1]); // project Edit renders first, the cluster's after it
    await waitFor(() => expect(screen.getByText("Compute fabric")).toBeTruthy());
    expect((screen.getByPlaceholderText("Detect (e.g. roce_vf_r)") as HTMLInputElement).value).toBe("");
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(catalogService.saveProject).toHaveBeenCalled());
    expect(savedProject().clusters[0].fabric).toEqual({ scaleout: "", addr_family: "", compute_hca: "" });
  });
});
