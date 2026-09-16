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
import type { ValidationPresetId } from "../lib/validationPresets";

function rack(id: string, nemo: string, opts: Partial<RackRow> = {}): RackRow {
  return {
    rack: id,
    nemo,
    last_job: "-",
    // Multirack selection requires 16 idle scheduler nodes; other rack trays may be unavailable.
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
      // Fewer than 16 idle nodes is not enough for a 16-node/rack validation.
      rack("ink50-dh01-d02", "done", { sinfo: "alloc=15,idle=1" }),
      rack("ink50-dh01-d03", "pending"),
      rack("ink50-dh01-e01", "done"),
      rack("ink50-dh01-e02", "fail"),
      rack("ink50-dh01-e03", "ongoing [12:34]"),
      // Exactly 16 idle nodes passes even though two other trays are unavailable.
      rack("ink50-dh01-e04", "done", { sinfo: "idle=16,down=2", ct_down_unk: "DN:e04-17,e04-18" }),
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
    // Fleet-wide: d01,e01,e04,orphan have at least 16 idle nodes -> 4.
    await waitFor(() => expect(screen.getByText("Select all eligible (4)")).toBeTruthy());

    fireEvent.change(screen.getByPlaceholderText("Filter by rack id…"), { target: { value: "d0" } });

    // Within "d0": only d01 is eligible; d02 has just one idle node and d03 is pending.
    await waitFor(() => expect(screen.getByText("Select all eligible (1)")).toBeTruthy());
    expect(screen.getByText(/1 eligible here/)).toBeTruthy();
  });

  it("select-all-eligible selects only the filtered racks, not the whole fleet", async () => {
    renderView();
    fireEvent.change(screen.getByPlaceholderText("Filter by rack id…"), { target: { value: "d0" } });
    await waitFor(() => expect(screen.getByText("Select all eligible (1)")).toBeTruthy());

    fireEvent.click(screen.getByText("Select all eligible (1)"));

    // The launch buttons must offer 1, not the 4 eligible fleet-wide.
    await waitFor(() => expect(screen.getByText("Launch Multirack Validation (1)")).toBeTruthy());
    expect(screen.getByText("1 selected")).toBeTruthy();
  });

  it("enables fallback validations while enforcing fixed Llama geometry", async () => {
    renderView();
    await waitFor(() => expect(screen.getByText("Select all eligible (4)")).toBeTruthy());
    fireEvent.click(screen.getByText("Select all eligible (4)"));

    const sysinfoButton = screen.getByRole("button", { name: "DGXC system_info" });
    expect(sysinfoButton).toBeEnabled();
    expect(screen.getByRole("button", { name: "GPT-OSS CPU — per-node fan-out" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: /Legacy Llama|512-GPU/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Llama 405B FP8 — 1024 GPUs / 50 steps" })).toBeDisabled();
    expect(screen.getByText(/documented Slurm fallback/i)).toBeInTheDocument();
  });

  it("makes MNUberGEMM planning available", async () => {
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
    fireEvent.click(screen.getByRole("checkbox", { name: "Select rack ink50-dh01-e01" }));

    const button = screen.getByRole("button", { name: "MNUberGEMM 26.03-v1.17" });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  });

  it.each([
    ["nccl-2.29.3", "NCCL 2.29.3 · 8K–32G · 500 iterations"],
    ["nemo-26.04.01", "NeMo 26.04.01"],
    ["mnubergemm-26.03-v1.17", "MNUberGEMM 26.03-v1.17"],
  ] as Array<[ValidationPresetId, string]>) (
    "submits one reviewed %s request containing all four selected racks",
    async (presetId, buttonLabel) => {
      const emptyBody = { ok: true, rows: [], jobs: [], active: false };
      const fetchMock = vi.mocked(globalThis.fetch).mockImplementation((input, init) => {
        const url = String(input);
        if (url === "/api/validation-presets") {
          return Promise.resolve(new Response(JSON.stringify({
            ok: true,
            presets: [{
              id: presetId,
              label: buttonLabel,
              description: "Four-rack acceptance recipe.",
              kind: presetId,
              defaults: { storage: "local", rsync: false },
              requirements: { scopes: ["rack", "multirack"] },
            }],
          }), { status: 200, headers: { "Content-Type": "application/json" } }));
        }
        if (url === "/api/validation-presets/prepare") {
          const request = JSON.parse(String(init?.body));
          return Promise.resolve(new Response(JSON.stringify({
            ok: true,
            plan: {
              ...request,
              summary: `One coordinated ${presetId} plan for four racks.`,
              node_count: 72,
              plan_hash: `reviewed-${presetId}`,
            },
          }), { status: 200, headers: { "Content-Type": "application/json" } }));
        }
        if (url === "/api/run-validation-preset-async") {
          return Promise.resolve(new Response(JSON.stringify({ ok: true, job_id: "one-activity" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }));
        }
        return Promise.resolve(new Response(JSON.stringify(emptyBody), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }));
      });

      renderView();
      await waitFor(() => expect(screen.getByText("Select all eligible (4)")).toBeInTheDocument());
      fireEvent.click(screen.getByText("Select all eligible (4)"));
      fireEvent.click(screen.getByRole("button", { name: buttonLabel }));
      await waitFor(() => expect(screen.getByRole("button", { name: "Review dry run" })).toBeEnabled());
      fireEvent.click(screen.getByRole("button", { name: "Review dry run" }));
      expect(await screen.findByText(`One coordinated ${presetId} plan for four racks.`)).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Launch validation" }));

      await waitFor(() => {
        const launchCalls = fetchMock.mock.calls.filter(([url]) => String(url) === "/api/run-validation-preset-async");
        expect(launchCalls).toHaveLength(1);
        const request = JSON.parse(String((launchCalls[0][1] as RequestInit).body));
        expect(request).toMatchObject({
          preset_id: presetId,
          scope: "multirack",
          plan_hash: `reviewed-${presetId}`,
        });
        expect(request.racks).toEqual([
          "ink50-dh01-d01",
          "ink50-dh01-e01",
          "ink50-dh01-e04",
          "orphan-rack-99",
        ]);
      });
    },
  );

  it("enables fixed Llama when a campaign resolves exactly 256 nodes", async () => {
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

  it("pins a NeMo launch to the reviewed 16-node campaign snapshot", async () => {
    const selectedRacks = ["ink50-dh01-d01", "ink50-dh01-e01"];
    vi.mocked(globalThis.fetch).mockImplementation((input, init) => {
      const url = String(input);
      const body = url === "/api/multirack-campaign"
        ? {
            ok: true,
            active: true,
            source: "live-scheduler",
            id: "live-16-reviewed",
            nodes_per_rack: 16,
            rack_count: 2,
            eligible_count: 2,
            racks: selectedRacks.map((rackId) => ({
              rack: rackId,
              eligible: true,
              reason: "",
              node_count: 16,
              nodes: Array.from({ length: 16 }, (_, index) => `${rackId}-${index + 1}`),
            })),
          }
        : url === "/api/run-nemo-xrack-async" && init?.method === "POST"
          ? { ok: true, job_id: "activity-1" }
          : { ok: true, rows: [], jobs: [] };
      return Promise.resolve(new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    });
    renderView();

    await waitFor(() => expect(screen.getByText("Select all eligible (2)")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Select all eligible (2)"));
    fireEvent.click(screen.getByRole("button", { name: "Launch Multirack Validation (2)" }));
    fireEvent.click(await screen.findByRole("button", { name: "Launch" }));

    await waitFor(() => {
      const launch = vi.mocked(globalThis.fetch).mock.calls.find(
        ([input, init]) => String(input) === "/api/run-nemo-xrack-async" && init?.method === "POST",
      );
      expect(launch).toBeDefined();
      expect(JSON.parse(String(launch?.[1]?.body))).toMatchObject({
        racks: selectedRacks,
        campaign: "live-16-reviewed",
      });
    });
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

    // Narrow to row d: e01, e04 and orphan-rack-99 stay selected but leave the screen.
    fireEvent.change(screen.getByPlaceholderText("Filter by rack id…"), { target: { value: "d0" } });

    await waitFor(() =>
      expect(
        screen.getByText(/3 selected racks are hidden by the current filter/)
      ).toBeTruthy()
    );
    // and the launch count still reflects all 4, which is why the warning exists
    expect(screen.getByText("Launch Multirack Validation (4)")).toBeTruthy();
  });

  it("no warning when every selected rack is visible", async () => {
    renderView();
    fireEvent.change(screen.getByPlaceholderText("Filter by rack id…"), { target: { value: "d0" } });
    await waitFor(() => expect(screen.getByText("Select all eligible (1)")).toBeTruthy());
    fireEvent.click(screen.getByText("Select all eligible (1)"));
    await waitFor(() => expect(screen.getByText("1 selected")).toBeTruthy());
    expect(screen.queryByText(/hidden by the current filter/)).toBeNull();
  });

  it("toggling select-all off clears only the filtered racks", async () => {
    renderView();
    await waitFor(() => expect(screen.getByText("Select all eligible (4)")).toBeTruthy());
    fireEvent.click(screen.getByText("Select all eligible (4)"));
    await waitFor(() => expect(screen.getByText("4 selected")).toBeTruthy());

    fireEvent.change(screen.getByPlaceholderText("Filter by rack id…"), { target: { value: "d0" } });
    await waitFor(() => expect(screen.getByText("Clear selection (1)")).toBeTruthy());
    fireEvent.click(screen.getByText("Clear selection (1)"));

    // d01 dropped; e01, e04 and orphan-rack-99 (selected, hidden) survive.
    await waitFor(() => expect(screen.getByText("3 selected")).toBeTruthy());
  });

  it("eligible-only hides racks that cannot be launched", async () => {
    renderView();
    await waitFor(() => expect(visibleRacks().length).toBe(8));

    fireEvent.click(screen.getByLabelText("Eligible only"));

    await waitFor(() => {
      const shown = visibleRacks();
      expect(shown).toEqual(["ink50-dh01-d01", "ink50-dh01-e01", "ink50-dh01-e04", "orphan-rack-99"]);
    });
  });

  it("eligible-only keeps a rack with 16 idle nodes despite two unavailable trays", async () => {
    renderView();
    fireEvent.click(screen.getByLabelText("Eligible only"));
    await waitFor(() => expect(visibleRacks()).toContain("ink50-dh01-e04"));
  });

  it("filters combine rather than replace each other", async () => {
    renderView();
    fireEvent.change(screen.getByPlaceholderText("Filter by rack id…"), { target: { value: "e0" } });
    fireEvent.click(screen.getByLabelText("Eligible only"));

    // Of e01..e04, e01 and e04 have at least 16 idle nodes.
    await waitFor(() => expect(visibleRacks()).toEqual(["ink50-dh01-e01", "ink50-dh01-e04"]));
  });
});
