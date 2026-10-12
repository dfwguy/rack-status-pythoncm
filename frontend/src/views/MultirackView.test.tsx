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
// MultirackView.test.tsx — the rack-picker filters on the Multirack Validation page.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// The picker lists every rack in the fleet (112 on Nscale TX, 5 pages), so "launch on row H"
// meant paging and hunting. These tests cover the filters and, more importantly, the two places
// where filtering can mislead an operator into launching on the wrong racks:
//   * "Select all eligible" must mean the racks currently on screen, not the whole fleet.
//   * A selection made before filtering stays in the launch count, so it must be surfaced.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MultirackView } from "./MultirackView";
import type { RackRow, RackStatusResponse } from "../types/api";

const catalogState = vi.hoisted(() => ({
  tools: [] as Array<{
    id: string;
    name: string;
    order: number;
    surfaces: string[];
    launch: { kind: string; preset_id: string };
  }>,
  loading: false,
  error: null as string | null,
}));

vi.mock("../hooks/useCatalogTools", () => ({
  useCatalogTools: () => ({
    tools: catalogState.tools,
    loading: catalogState.loading,
    error: catalogState.error,
    reload: vi.fn(),
  }),
}));

function compatibleTools() {
  const names: Record<string, string> = {
    "nemo-xrack": "Launch Multirack Validation",
    "preset-nccl": "NCCL 2.31.2 / CUDA 13.3+",
    "preset-llama405b": "Llama 405B FP8 — 1024 GPUs / 50 steps",
    "preset-gpt-oss": "GPT-OSS CPU — per-node fan-out",
    "preset-exemplar-sysinfo": "DGXC system_info",
    "preset-nemo": "NeMo 26.04.01",
    "preset-mnubergemm": "MNUberGEMM 26.03-v1.17",
    "clusterkit-multirack": "ClusterKit Rack-to-Rack",
  };
  return [
    { id: "nemo-xrack", surfaces: ["multirack"], launch: { kind: "async-job", preset_id: "" } },
    { id: "preset-nccl", surfaces: ["multirack"], launch: { kind: "validation-preset", preset_id: "nccl-2.31.2" } },
    { id: "preset-llama405b", surfaces: ["multirack"], launch: { kind: "validation-preset", preset_id: "llama31-405b-fp8" } },
    { id: "preset-gpt-oss", surfaces: ["multirack"], launch: { kind: "validation-preset", preset_id: "gpt-oss-cpu" } },
    { id: "preset-exemplar-sysinfo", surfaces: ["multirack"], launch: { kind: "validation-preset", preset_id: "exemplar-system-info" } },
    { id: "preset-nemo", surfaces: ["multirack"], launch: { kind: "validation-preset", preset_id: "nemo-26.04.01" } },
    { id: "preset-mnubergemm", surfaces: ["multirack"], launch: { kind: "validation-preset", preset_id: "mnubergemm-26.03-v1.17" } },
    { id: "clusterkit-multirack", surfaces: ["multirack"], launch: { kind: "async-job", preset_id: "" } },
  ].map((tool, order) => ({ ...tool, name: names[tool.id], order }));
}

function rack(id: string, nemo: string, opts: Partial<RackRow> = {}): RackRow {
  return {
    rack: id,
    nemo,
    last_job: "-",
    // Eligibility accepts idle or yieldable allocated sinfo and no DN trays; overridden below.
    sinfo: "idle=18",
    ct_down_unk: "-",
    ...opts,
  } as RackRow;
}

/** Two rows (d, e) with a mix of statuses, plus one rack the backend never placed in a row. */
function makeData(): RackStatusResponse {
  return {
    ok: true,
    rows: [
      rack("ink50-dh01-d01", "done"),
      // Bring-up diagnostics may still own trays; the launch-side preflight verifies
      // that allocated jobs are exclusively yieldable bringup-* jobs.
      rack("ink50-dh01-d02", "done", { sinfo: "alloc=15,idle=1" }),
      rack("ink50-dh01-d03", "pending"),
      rack("ink50-dh01-e01", "done"),
      rack("ink50-dh01-e02", "fail"),
      rack("ink50-dh01-e03", "ongoing [12:34]"),
      // eligible-looking status but a drained tray -> not eligible
      rack("ink50-dh01-e04", "done", { sinfo: "idle=17,drain=1" }),
      rack("orphan-rack-99", "done"),
    ],
    hierarchy: {
      levels: ["section", "row", "rack"],
      sections: [
        {
          key: "ink50-dh01",
          label: "dh01",
          rows: [
            {
              key: "ink50-dh01-d",
              label: "d",
              racks: [
                { rack: "ink50-dh01-d01", label: "d01" },
                { rack: "ink50-dh01-d02", label: "d02" },
                { rack: "ink50-dh01-d03", label: "d03" },
              ],
            },
            {
              key: "ink50-dh01-e",
              label: "e",
              racks: [
                { rack: "ink50-dh01-e01", label: "e01" },
                { rack: "ink50-dh01-e02", label: "e02" },
                { rack: "ink50-dh01-e03", label: "e03" },
                { rack: "ink50-dh01-e04", label: "e04" },
              ],
            },
          ],
        },
      ],
      unmatched: ["orphan-rack-99"],
    },
  } as unknown as RackStatusResponse;
}

beforeEach(() => {
  catalogState.tools = compatibleTools();
  catalogState.loading = false;
  catalogState.error = null;
  // The view polls multirack jobs and on-disk results on mount; keep both empty and quiet.
  const emptyBody = JSON.stringify({ ok: true, rows: [], jobs: [] });
  vi.spyOn(globalThis, "fetch").mockResolvedValue({
    ok: true,
    status: 200,
    text: async () => emptyBody,
    json: async () => JSON.parse(emptyBody),
  } as unknown as Response);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function renderView() {
  return render(<MultirackView data={makeData()} logDir="/logs" />);
}

/** Rack ids currently rendered in the picker table. */
function visibleRacks(): string[] {
  return screen
    .getAllByText(/^(ink50-dh01-[de]\d\d|orphan-rack-99)$/)
    .map((el) => el.textContent || "");
}

/**
 * Pick an option in a KUI Combobox. Its listbox lives in a `popover="manual"` element, so it is
 * outside the accessibility tree until opened and `getByRole("option")` cannot see it -- hence
 * `hidden: true`. Clicking the option still drives `onValueChange`.
 */
function chooseOption(comboboxLabel: string, optionText: string) {
  const combobox = screen.getByLabelText(comboboxLabel);
  const listboxId = combobox.getAttribute("aria-controls");
  const options = screen
    .getAllByRole("option", { hidden: true })
    .filter((o) => o.closest(`#${listboxId}`) !== null);
  const match = options.find((o) => o.textContent === optionText);
  if (!match) {
    throw new Error(
      `option "${optionText}" not in ${comboboxLabel}; saw ${JSON.stringify(options.map((o) => o.textContent))}`
    );
  }
  fireEvent.click(match);
}

describe("MultirackView rack filters", () => {
  it("renders a newly catalogued preset and its edited name without frontend registration", () => {
    catalogState.tools.push({
      id: "site-preset", name: "Site acceptance recipe", order: 99,
      surfaces: ["multirack"], launch: { kind: "validation-preset", preset_id: "site-recipe" },
    });
    catalogState.tools[1].name = "Our NCCL recipe";
    renderView();
    expect(screen.getByRole("button", { name: "Site acceptance recipe" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Our NCCL recipe" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Select all eligible/ }));
    expect(screen.getByRole("button", { name: "Site acceptance recipe" })).toBeEnabled();
    expect(screen.getByRole("button", { name: /Llama 405B FP8/ })).toBeDisabled();
  });

  it.each(["loading", "error"])("hides stale catalog actions on catalog %s", (state) => {
    if (state === "loading") catalogState.loading = true;
    else catalogState.error = "catalog unavailable";
    renderView();
    for (const tool of compatibleTools()) {
      expect(screen.queryByRole("button", { name: new RegExp(tool.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")) })).toBeNull();
    }
    if (state === "error") expect(screen.getByText(/Could not load compatible tools/)).toBeInTheDocument();
  });

  it("does not render a recipe assigned only to another surface", () => {
    catalogState.tools[1].surfaces = ["rackDetail.validation"];
    renderView();
    expect(screen.queryByRole("button", { name: "NCCL 2.31.2 / CUDA 13.3+" })).toBeNull();
  });

  it("hides NeMo launch actions when the catalog marks them incompatible", async () => {
    catalogState.tools = compatibleTools().filter(
      (tool) => tool.id !== "nemo-xrack" && tool.id !== "preset-nemo",
    );

    renderView();
    await waitFor(() => expect(visibleRacks().length).toBe(8));

    expect(screen.queryByRole("button", { name: /Launch Multirack Validation/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "NeMo 26.04.01" })).toBeNull();
    expect(screen.getByRole("button", { name: "GPT-OSS CPU — per-node fan-out" })).toBeInTheDocument();
  });

  it("shows every rack before any filter is applied", async () => {
    renderView();
    await waitFor(() => expect(visibleRacks().length).toBe(8));
  });

  it("filters by rack id text", async () => {
    renderView();
    await waitFor(() => expect(visibleRacks().length).toBe(8));

    fireEvent.change(screen.getByPlaceholderText("Filter by rack id…"), {
      target: { value: "e0" },
    });

    await waitFor(() => {
      const shown = visibleRacks();
      expect(shown).toEqual(["ink50-dh01-e01", "ink50-dh01-e02", "ink50-dh01-e03", "ink50-dh01-e04"]);
    });
    expect(screen.getByText(/4 of 8 racks shown/)).toBeTruthy();
  });

  it("text filter is case-insensitive", async () => {
    renderView();
    fireEvent.change(screen.getByPlaceholderText("Filter by rack id…"), {
      target: { value: "INK50-DH01-D01" },
    });
    await waitFor(() => expect(visibleRacks()).toEqual(["ink50-dh01-d01"]));
  });

  it("offers a row filter built from the backend hierarchy", async () => {
    renderView();
    // The hierarchy defines rows d and e; the row control only renders when rows exist.
    await waitFor(() => expect(screen.getByLabelText("Filter by datacenter row")).toBeTruthy());
    const listboxId = screen.getByLabelText("Filter by datacenter row").getAttribute("aria-controls");
    const rowOpts = screen
      .getAllByRole("option", { hidden: true })
      .filter((o) => o.closest(`#${listboxId}`) !== null)
      .map((o) => o.textContent);
    expect(rowOpts).toEqual(["d", "e"]);
  });

  it("filters to a single datacenter row", async () => {
    renderView();
    await waitFor(() => expect(visibleRacks().length).toBe(8));

    chooseOption("Filter by datacenter row", "d");

    await waitFor(() =>
      expect(visibleRacks()).toEqual(["ink50-dh01-d01", "ink50-dh01-d02", "ink50-dh01-d03"])
    );
    // orphan-rack-99 has no row in the hierarchy, so a row filter must exclude it.
    expect(visibleRacks()).not.toContain("orphan-rack-99");
  });

  it("row filter accumulates rather than replacing", async () => {
    renderView();
    chooseOption("Filter by datacenter row", "d");
    await waitFor(() => expect(visibleRacks().length).toBe(3));
    chooseOption("Filter by datacenter row", "e");
    await waitFor(() => expect(visibleRacks().length).toBe(7));
  });

  it("filters by validation status, matching the timed ongoing form", async () => {
    renderView();
    await waitFor(() => expect(visibleRacks().length).toBe(8));

    // "ongoing [12:34]" must match the "ongoing" bucket -- raw equality would miss it.
    chooseOption("Filter by validation status", "ongoing");

    await waitFor(() => expect(visibleRacks()).toEqual(["ink50-dh01-e03"]));
  });

  it("filters by validation status done", async () => {
    renderView();
    chooseOption("Filter by validation status", "done");
    await waitFor(() =>
      expect(visibleRacks()).toEqual([
        "ink50-dh01-d01",
        "ink50-dh01-d02",
        "ink50-dh01-e01",
        "ink50-dh01-e04",
        "orphan-rack-99",
      ])
    );
  });

  it("combines a row filter with a validation filter", async () => {
    renderView();
    chooseOption("Filter by datacenter row", "e");
    chooseOption("Filter by validation status", "done");
    // Row e, status done: e01 and e04 (e04 is done but drained, so still listed here).
    await waitFor(() => expect(visibleRacks()).toEqual(["ink50-dh01-e01", "ink50-dh01-e04"]));
  });

  it("reports when a filter matches nothing and offers a way out", async () => {
    renderView();
    fireEvent.change(screen.getByPlaceholderText("Filter by rack id…"), {
      target: { value: "no-such-rack" },
    });
    await waitFor(() => expect(screen.getByText("No racks match the current filter.")).toBeTruthy());
    // Both the empty state and the toolbar offer a clear; either must restore the full list.
    fireEvent.click(screen.getAllByText("Clear filters")[0]);
    await waitFor(() => expect(visibleRacks().length).toBe(8));
  });

  it("clearing filters restores every rack", async () => {
    renderView();
    fireEvent.change(screen.getByPlaceholderText("Filter by rack id…"), { target: { value: "d0" } });
    await waitFor(() => expect(visibleRacks().length).toBe(3));
    fireEvent.click(screen.getByText("Clear filters"));
    await waitFor(() => expect(visibleRacks().length).toBe(8));
  });

  it("counts only eligible racks inside the current filter", async () => {
    renderView();
    // Fleet-wide: d01,d02,e01,orphan are eligible (e04 is drained, others not done) -> 4.
    await waitFor(() => expect(screen.getByText("Select all eligible (4)")).toBeTruthy());

    fireEvent.change(screen.getByPlaceholderText("Filter by rack id…"), { target: { value: "d0" } });

    // Within "d0": d01 and d02 are eligible, d03 is pending.
    await waitFor(() => expect(screen.getByText("Select all eligible (2)")).toBeTruthy());
    expect(screen.getByText(/2 eligible here/)).toBeTruthy();
  });

  it("select-all-eligible selects only the filtered racks, not the whole fleet", async () => {
    renderView();
    fireEvent.change(screen.getByPlaceholderText("Filter by rack id…"), { target: { value: "d0" } });
    await waitFor(() => expect(screen.getByText("Select all eligible (2)")).toBeTruthy());

    fireEvent.click(screen.getByText("Select all eligible (2)"));

    // The launch buttons must offer 2, not the 4 eligible fleet-wide.
    await waitFor(() => expect(screen.getByText("Launch Multirack Validation (2)")).toBeTruthy());
    expect(screen.getByText("2 selected")).toBeTruthy();
  });

  it("offers DGXC system_info and GPT-OSS per-node fanout for selected racks", async () => {
    renderView();
    await waitFor(() => expect(screen.getByText("Select all eligible (4)")).toBeTruthy());
    fireEvent.click(screen.getByText("Select all eligible (4)"));

    const sysinfoButton = screen.getByRole("button", { name: "DGXC system_info" });
    expect(sysinfoButton).toBeEnabled();
    expect(screen.getByRole("button", { name: "GPT-OSS CPU — per-node fan-out" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /Legacy Llama|512-GPU/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Llama 405B FP8 — 1024 GPUs / 50 steps" })).toBeDisabled();
    fireEvent.click(sysinfoButton);

    expect(await screen.findByRole("dialog", { name: "NVIDIA DGXC v26.06.01 system_info" })).toBeInTheDocument();
    expect(screen.getByText(/fans out to every selected node/i)).toBeInTheDocument();
  });

  it("offers MNUberGEMM for a multirack selection and prepares the exact request", async () => {
    const emptyBody = { ok: true, rows: [], jobs: [] };
    vi.mocked(globalThis.fetch).mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/validation-presets") {
        return Promise.resolve(new Response(JSON.stringify({
          ok: true,
          presets: [{
            id: "mnubergemm-26.03-v1.17",
            label: "MNUberGEMM 26.03 v1.17",
            description: "Multi-rack MNUberGEMM validation.",
            kind: "mnubergemm",
            defaults: { storage: "local", rsync: true },
            requirements: { scopes: ["node", "rack", "multirack"] },
          }],
        }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      if (url === "/api/validation-presets/prepare") {
        const request = JSON.parse(String(init?.body));
        return Promise.resolve(new Response(JSON.stringify({
          ok: true,
          plan: {
            ...request,
            summary: "MNUberGEMM as two independent rack jobs",
            node_count: 36,
            plan_hash: "reviewed-mnubergemm-plan",
          },
        }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      return Promise.resolve(new Response(JSON.stringify(emptyBody), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    });
    renderView();
    await waitFor(() => expect(screen.getByText("Select all eligible (4)")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("checkbox", { name: "Select rack ink50-dh01-d01" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select rack ink50-dh01-d02" }));

    const button = screen.getByRole("button", { name: "MNUberGEMM 26.03-v1.17" });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    expect(await screen.findByRole("dialog", { name: "MNUberGEMM 26.03 v1.17" })).toBeInTheDocument();
    expect(screen.getByText(/one independent, rack-scoped MNUberGEMM job per selected rack/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Review dry run" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Review dry run" }));
    await waitFor(() => expect(screen.getByText("MNUberGEMM as two independent rack jobs")).toBeInTheDocument());

    const prepareCall = vi.mocked(globalThis.fetch).mock.calls.find(([url]) => String(url) === "/api/validation-presets/prepare");
    expect(prepareCall).toBeDefined();
    expect(JSON.parse(String(prepareCall?.[1]?.body))).toEqual({
      preset_id: "mnubergemm-26.03-v1.17",
      scope: "multirack",
      racks: ["ink50-dh01-d01", "ink50-dh01-d02"],
      log_dir: "/logs",
      storage: "local",
      rsync_to_local: true,
    });
  });

  it("launches ClusterKit rack-to-rack on the selected racks through prepare -> review", async () => {
    vi.mocked(globalThis.fetch).mockImplementation((input) => {
      const url = String(input);
      const body = url === "/api/launch/prepare"
        ? { ok: true, kind: "clusterkit", plan: {}, plan_hash: "reviewed-clusterkit-plan" }
        : url === "/api/run-clusterkit-async"
          ? { ok: true, job_id: "ck-1" }
          : { ok: true, rows: [], jobs: [] };
      return Promise.resolve(new Response(JSON.stringify(body), {
        status: 200, headers: { "Content-Type": "application/json" },
      }));
    });
    renderView();
    await waitFor(() => expect(screen.getByText("Select all eligible (4)")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("checkbox", { name: "Select rack ink50-dh01-d01" }));
    // One rack is not a rack-to-rack test.
    expect(screen.getByRole("button", { name: "ClusterKit Rack-to-Rack (1)" })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: "Select rack ink50-dh01-d02" }));
    fireEvent.click(screen.getByRole("button", { name: "ClusterKit Rack-to-Rack (2)" }));

    await waitFor(() => expect(
      vi.mocked(globalThis.fetch).mock.calls.some(([url]) => String(url) === "/api/run-clusterkit-async"),
    ).toBe(true));
    const calls = vi.mocked(globalThis.fetch).mock.calls;
    const prepare = calls.find(([url]) => String(url) === "/api/launch/prepare");
    expect(JSON.parse(String(prepare?.[1]?.body))).toEqual({
      kind: "clusterkit", racks: ["ink50-dh01-d01", "ink50-dh01-d02"], log_dir: "/logs",
    });
    const launch = calls.find(([url]) => String(url) === "/api/run-clusterkit-async");
    expect(JSON.parse(String(launch?.[1]?.body))).toEqual({
      racks: ["ink50-dh01-d01", "ink50-dh01-d02"], log_dir: "/logs", plan_hash: "reviewed-clusterkit-plan",
    });
  });

  it("enables fixed Llama 1024 only when the exact-node campaign resolves 256 nodes", async () => {
    const campaignRacks = Array.from({ length: 16 }, (_, index) => `rack-${String(index + 1).padStart(2, "0")}`);
    const emptyBody = { ok: true, rows: [], jobs: [] };
    vi.mocked(globalThis.fetch).mockImplementation((input) => {
      if (String(input) === "/api/multirack-campaign") {
        return Promise.resolve({
          ok: true,
          status: 200,
          text: async () => JSON.stringify({
            ok: true,
            active: true,
            id: "exemplar-16",
            nodes_per_rack: 16,
            rack_count: 16,
            eligible_count: 16,
            racks: campaignRacks.map((rackId, rackIndex) => ({
              rack: rackId,
              eligible: true,
              reason: "",
              node_count: 16,
              nodes: Array.from({ length: 16 }, (_, nodeIndex) => `${rackId}-n${rackIndex}-${nodeIndex}`),
            })),
          }),
          json: async () => ({
            ok: true,
            active: true,
            id: "exemplar-16",
            nodes_per_rack: 16,
            rack_count: 16,
            eligible_count: 16,
            racks: campaignRacks.map((rackId, rackIndex) => ({
              rack: rackId,
              eligible: true,
              reason: "",
              node_count: 16,
              nodes: Array.from({ length: 16 }, (_, nodeIndex) => `${rackId}-n${rackIndex}-${nodeIndex}`),
            })),
          }),
        } as Response);
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        text: async () => JSON.stringify(emptyBody),
        json: async () => emptyBody,
      } as Response);
    });
    const rows = campaignRacks.map((id) => rack(id, "done"));
    render(<MultirackView data={{ ...makeData(), rows, hierarchy: undefined } as RackStatusResponse} logDir="/logs" />);

    await waitFor(() => expect(screen.getByText("Select all eligible (16)")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Select all eligible (16)"));
    expect(screen.getByRole("button", { name: "Llama 405B FP8 — 1024 GPUs / 50 steps" })).toBeEnabled();
    expect(screen.getByText(/16 selected nodes per rack/i)).toBeInTheDocument();
  });

  it("does not infer 256 exact nodes from rack count times campaign geometry", async () => {
    const campaignRacks = Array.from({ length: 16 }, (_, index) => `rack-${String(index + 1).padStart(2, "0")}`);
    vi.mocked(globalThis.fetch).mockImplementation((input) => {
      const body = String(input) === "/api/multirack-campaign"
        ? {
            ok: true,
            active: true,
            id: "exemplar-16",
            nodes_per_rack: 16,
            racks: campaignRacks.map((rackId) => ({ rack: rackId, eligible: true, reason: "", node_count: 15 })),
          }
        : { ok: true, rows: [], jobs: [] };
      return Promise.resolve(new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    });
    render(
      <MultirackView
        data={{ ...makeData(), rows: campaignRacks.map((id) => rack(id, "done")), hierarchy: undefined } as RackStatusResponse}
        logDir="/logs"
      />,
    );

    await waitFor(() => expect(screen.getByText("Select all eligible (16)")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Select all eligible (16)"));
    expect(screen.getByRole("button", { name: "Llama 405B FP8 — 1024 GPUs / 50 steps" })).toBeDisabled();
    expect(screen.getByText(/240 manifest nodes are selected/i)).toBeInTheDocument();
  });

  it("gives every rack selection checkbox an accessible name", async () => {
    renderView();
    expect(await screen.findByRole("checkbox", { name: "Select rack ink50-dh01-d01" })).toBeInTheDocument();
  });

  it("warns when a selected rack is hidden by the filter", async () => {
    renderView();
    await waitFor(() => expect(screen.getByText("Select all eligible (4)")).toBeTruthy());
    fireEvent.click(screen.getByText("Select all eligible (4)"));
    await waitFor(() => expect(screen.getByText("4 selected")).toBeTruthy());

    // Narrow to row d: the eligible e01 and orphan-rack-99 stay selected but leave the screen.
    fireEvent.change(screen.getByPlaceholderText("Filter by rack id…"), { target: { value: "d0" } });

    await waitFor(() =>
      expect(
        screen.getByText(/2 selected racks are hidden by the current filter/)
      ).toBeTruthy()
    );
    // and the launch count still reflects all 4, which is why the warning exists
    expect(screen.getByText("Launch Multirack Validation (4)")).toBeTruthy();
  });

  it("no warning when every selected rack is visible", async () => {
    renderView();
    fireEvent.change(screen.getByPlaceholderText("Filter by rack id…"), { target: { value: "d0" } });
    await waitFor(() => expect(screen.getByText("Select all eligible (2)")).toBeTruthy());
    fireEvent.click(screen.getByText("Select all eligible (2)"));
    await waitFor(() => expect(screen.getByText("2 selected")).toBeTruthy());
    expect(screen.queryByText(/hidden by the current filter/)).toBeNull();
  });

  it("toggling select-all off clears only the filtered racks", async () => {
    renderView();
    await waitFor(() => expect(screen.getByText("Select all eligible (4)")).toBeTruthy());
    fireEvent.click(screen.getByText("Select all eligible (4)"));
    await waitFor(() => expect(screen.getByText("4 selected")).toBeTruthy());

    fireEvent.change(screen.getByPlaceholderText("Filter by rack id…"), { target: { value: "d0" } });
    await waitFor(() => expect(screen.getByText("Clear selection (2)")).toBeTruthy());
    fireEvent.click(screen.getByText("Clear selection (2)"));

    // d01+d02 dropped; e01 and orphan-rack-99 (selected, hidden) survive.
    await waitFor(() => expect(screen.getByText("2 selected")).toBeTruthy());
  });

  it("eligible-only hides racks that cannot be launched", async () => {
    renderView();
    await waitFor(() => expect(visibleRacks().length).toBe(8));

    fireEvent.click(screen.getByLabelText("Eligible only"));

    await waitFor(() => {
      const shown = visibleRacks();
      expect(shown).toEqual(["ink50-dh01-d01", "ink50-dh01-d02", "ink50-dh01-e01", "orphan-rack-99"]);
    });
  });

  it("eligible-only excludes a rack whose trays are drained", async () => {
    renderView();
    fireEvent.click(screen.getByLabelText("Eligible only"));
    // e04 is "done" but has drain=1 in sinfo.
    await waitFor(() => expect(visibleRacks()).not.toContain("ink50-dh01-e04"));
  });

  it("judges a translate cluster's racks by their placed nodes, not the up/down sinfo tally", async () => {
    // Nscale NC: hostnames name no rack, the row's sinfo cell is BCM's "up=18" and ct_down_unk is "-".
    const ncNodes = (rackId: string, first: number, drained = 0) =>
      Array.from({ length: 18 }, (_, i) => ({
        id: `sys1-gso1-p-phy-gpu${first + i}-su19`, rack: rackId, status: "up", ip: "",
        scheduler_state: i < drained ? "drain" : "idle",
      }));
    const data = {
      ok: true,
      rows: [
        rack("ink50-dh01-d01", "done", { sinfo: "up=18" }),
        rack("ink50-dh01-d02", "done", { sinfo: "up=18" }),
      ],
      nodes: [...ncNodes("ink50-dh01-d01", 19), ...ncNodes("ink50-dh01-d02", 37, 2)],
    } as unknown as RackStatusResponse;
    render(<MultirackView data={data} logDir="/logs" />);
    fireEvent.click(screen.getByLabelText("Eligible only"));
    await waitFor(() => expect(visibleRacks()).toEqual(["ink50-dh01-d01"]));
    expect(screen.getByText("1 eligible")).toBeInTheDocument();
  });

  it("filters combine rather than replace each other", async () => {
    renderView();
    fireEvent.change(screen.getByPlaceholderText("Filter by rack id…"), { target: { value: "e0" } });
    fireEvent.click(screen.getByLabelText("Eligible only"));

    // Of e01..e04 only e01 is eligible.
    await waitFor(() => expect(visibleRacks()).toEqual(["ink50-dh01-e01"]));
  });
});
