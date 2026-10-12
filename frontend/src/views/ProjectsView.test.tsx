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
  clusterkit_tests: ["lat", "bw", "gpu_gpu_lat", "gpu_gpu_bw"],
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

  it("names the container's NCCL net plugin for the cluster and saves it", async () => {
    await openNewCluster();
    fireEvent.change(screen.getByPlaceholderText("e.g. Pod A"), { target: { value: "NC" } });
    fireEvent.change(
      screen.getByPlaceholderText("NCCL default (e.g. /opt/hpcx/nccl_spectrum-x_plugin/lib/libnccl-net-spcx.so)"),
      { target: { value: "/opt/hpcx/nccl_spectrum-x_plugin/lib/libnccl-net-spcx.so" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(catalogService.saveProject).toHaveBeenCalled());
    expect(savedProject().clusters[0].fabric?.net_plugin).toBe("/opt/hpcx/nccl_spectrum-x_plugin/lib/libnccl-net-spcx.so");
  });

  it("states the fabric overrides on a new cluster and saves them", async () => {
    await openNewCluster();
    fireEvent.change(screen.getByPlaceholderText("e.g. Pod A"), { target: { value: "NC" } });
    choose(/RoCE address family/i, /IPv6/);
    fireEvent.change(screen.getByPlaceholderText("Detect (e.g. roce_vf_r)"), { target: { value: "roce_vf_r" } });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(catalogService.saveProject).toHaveBeenCalled());
    expect(savedProject().clusters[0].fabric).toEqual({ scaleout: "", addr_family: "ipv6", compute_hca: "roce_vf_r", rail_rate_gbps: 0 , net_plugin: "" });
  });

  it("states the rail rate and saves it as a number", async () => {
    await openNewCluster();
    fireEvent.change(screen.getByPlaceholderText("e.g. Pod A"), { target: { value: "NC" } });
    choose(/Fabric network/i, /^RoCE$/);
    fireEvent.change(screen.getByPlaceholderText("0 = derive (e.g. 200)"), { target: { value: "200" } });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(catalogService.saveProject).toHaveBeenCalled());
    expect(savedProject().clusters[0].fabric).toEqual({ scaleout: "roce", addr_family: "", compute_hca: "", rail_rate_gbps: 200 , net_plugin: "" });
  });

  it("states the cluster's fabric network and saves it", async () => {
    await openNewCluster();
    fireEvent.change(screen.getByPlaceholderText("e.g. Pod A"), { target: { value: "NC" } });
    choose(/Fabric network/i, /^RoCE$/);
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(catalogService.saveProject).toHaveBeenCalled());
    expect(savedProject().clusters[0].fabric).toEqual({ scaleout: "roce", addr_family: "", compute_hca: "", rail_rate_gbps: 0 , net_plugin: "" });
  });

  it("saves a new cluster with node commands as before: through the head node, without sudo", async () => {
    await openNewCluster();
    fireEvent.change(screen.getByPlaceholderText("e.g. Pod A"), { target: { value: "NC" } });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(catalogService.saveProject).toHaveBeenCalled());
    const conn = savedProject().clusters[0].connection;
    expect([conn.node_sudo, conn.node_exec_direct]).toEqual([false, false]);
  });

  it("turns on sudo -n and direct node access for a cluster and saves both", async () => {
    await openNewCluster();
    fireEvent.change(screen.getByPlaceholderText("e.g. Pod A"), { target: { value: "NC" } });
    fireEvent.click(screen.getByRole("switch", { name: "Node commands" }));
    fireEvent.click(screen.getByRole("switch", { name: "Reach compute nodes" }));
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(catalogService.saveProject).toHaveBeenCalled());
    const conn = savedProject().clusters[0].connection;
    expect([conn.node_sudo, conn.node_exec_direct]).toEqual([true, true]);
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
    expect(savedProject().clusters[0].fabric).toEqual({ scaleout: "", addr_family: "", compute_hca: "", rail_rate_gbps: 0 , net_plugin: "" });
  });
});

describe("NCCL environment", () => {
  const savedProject = () => vi.mocked(catalogService.saveProject).mock.calls.at(-1)?.[0] as Project;
  const span = (label: string) => screen.getByRole("textbox", { name: label }) as HTMLTextAreaElement;

  it("saves each span's KEY=VALUE lines as its own list, ignoring lines that are not variables", async () => {
    await openNewCluster();
    fireEvent.change(screen.getByPlaceholderText("e.g. Pod A"), { target: { value: "NC" } });
    fireEvent.change(span("Multi-rack"), {
      target: { value: "NCCL_NET_PLUGIN=/opt/spcx.so\n# a note\nNCCL_IB_DYNAMIC_FLOW=1\nnot a variable" },
    });
    fireEvent.change(span("Single rack"), { target: { value: "NCCL_MIN_NCHANNELS=16" } });
    expect(screen.getByText(/2 variables\. 1 line ignored/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(catalogService.saveProject).toHaveBeenCalled());
    expect(savedProject().clusters[0].nccl_env).toEqual({
      node: {},
      rack: { NCCL_MIN_NCHANNELS: "16" },
      multirack: { NCCL_NET_PLUGIN: "/opt/spcx.so", NCCL_IB_DYNAMIC_FLOW: "1" },
    });
  });

  it("keeps a half-typed line in the box while it is not yet a variable", async () => {
    await openNewCluster();
    fireEvent.change(span("Single node"), { target: { value: "NCCL_" } });
    expect(span("Single node").value).toBe("NCCL_");
  });

  it("shows a stored list, and a cluster stored without one saves the span it was given", async () => {
    const stored = {
      id: "nc",
      name: "NC",
      platform_id: "",
      cluster_manager: "bcm",
      scheduler: "slurm",
      rack_mapping_mode: "translate",
      site_prefix: "",
      naming: { rack_id_template: "", ct_id_template: "", nvsw_id_template: "", ct_joiner: "-ct", slurm_hostlist: "" },
      geometry: { pods: 1, rows_per_pod: 0, racks_per_row: 0, trays_per_rack: 0 },
      fabric: { scaleout: "roce", addr_family: "", compute_hca: "", rail_rate_gbps: 0 },
      // No nccl_env: a backend from before the field omits it.
      connection: {
        exec_mode: "", ssh_host: "", ssh_user: "", ssh_key: "", remote_base_path: "",
        log_dir: "", workspace_dir: "", shared_container_dir: "", node_container_dir: "",
      },
      scalable_units: [],
      campaigns: [],
      evidence: { snapshot_path: "", healthcheck_root: "" },
      p2p_enabled: false,
    };
    const withEnv = { ...stored, id: "nc2", name: "NC2", nccl_env: { node: {}, rack: {}, multirack: { NCCL_IB_DYNAMIC_FLOW: "1" } } };
    const project = { ...PROJECT, clusters: [stored, withEnv] } as unknown as Project;
    vi.mocked(catalogService.getVocab).mockResolvedValue(VOCAB);
    vi.mocked(catalogService.listPlatforms).mockResolvedValue([]);
    render(
      <ActiveClusterContext.Provider value={{ ...clusterStub(), projects: [project] }}>
        <ProjectsView />
      </ActiveClusterContext.Provider>,
    );
    const edits = await screen.findAllByRole("button", { name: /^Edit$/ });
    // Project Edit renders first, then one per cluster in order.
    fireEvent.click(edits[edits.length - 1]);
    await waitFor(() => expect(screen.getByText("NCCL environment")).toBeTruthy());
    expect(span("Multi-rack").value).toBe("NCCL_IB_DYNAMIC_FLOW=1");
    fireEvent.click(screen.getByRole("button", { name: /^Cancel$/ }));

    fireEvent.click((await screen.findAllByRole("button", { name: /^Edit$/ }))[1]);
    await waitFor(() => expect(screen.getByText("NCCL environment")).toBeTruthy());
    expect(span("Multi-rack").value).toBe("");
    fireEvent.change(span("Multi-rack"), { target: { value: "NCCL_IB_DYNAMIC_FLOW=1" } });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(catalogService.saveProject).toHaveBeenCalled());
    expect(savedProject().clusters[0].nccl_env).toEqual({ node: {}, rack: {}, multirack: { NCCL_IB_DYNAMIC_FLOW: "1" } });
  });
});

describe("ClusterKit", () => {
  const savedProject = () => vi.mocked(catalogService.saveProject).mock.calls.at(-1)?.[0] as Project;

  it("saves a new cluster with no ClusterKit choice, so the launcher's defaults apply", async () => {
    await openNewCluster();
    fireEvent.change(screen.getByPlaceholderText("e.g. Pod A"), { target: { value: "NC" } });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(catalogService.saveProject).toHaveBeenCalled());
    expect(savedProject().clusters[0].clusterkit).toEqual({ tests: [], gpudirect: false });
  });

  it("states the test cases and GPU-Direct for the cluster and saves them", async () => {
    await openNewCluster();
    fireEvent.change(screen.getByPlaceholderText("e.g. Pod A"), { target: { value: "NC" } });
    const tests = screen.getByPlaceholderText("Default (bw, lat)");
    for (const name of ["gpu_gpu_bw", "gpu_gpu_lat"]) {
      fireEvent.focus(tests);
      fireEvent.change(tests, { target: { value: name } });
      const listbox = tests.getAttribute("aria-controls");
      const options = (await screen.findAllByRole("option", { name, hidden: true })).filter((o) =>
        listbox ? o.closest(`#${CSS.escape(listbox)}`) !== null : true,
      );
      fireEvent.click(options[0]);
    }
    fireEvent.click(screen.getByRole("switch", { name: "GPU tests" }));
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(catalogService.saveProject).toHaveBeenCalled());
    expect(savedProject().clusters[0].clusterkit).toEqual({ tests: ["gpu_gpu_bw", "gpu_gpu_lat"], gpudirect: true });
  });
});
