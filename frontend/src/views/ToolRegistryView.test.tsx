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
 * `ToolRegistryView` — platform compatibility.
 *
 * The subject is the gap between what the backend resolves and what the form let an operator
 * say. `ToolCompatibility.matches` checks `platform_ids`, then `capabilities`, then `match` — so
 * the catalog has always been able to scope a tool to named platforms. The form offered "Match:
 * all platforms / none (disabled)" beside a free-text CSV box, which reads as though "all or
 * nothing" is the whole choice.
 *
 * Two failures here are silent, which is why they are tested rather than eyeballed:
 *
 * 1. Choosing "only selected platforms" and selecting none writes an empty `platform_ids`, which
 *    falls through to `match: "all"` — the tool is offered *everywhere*, the opposite of the ask.
 * 2. Switching a scoped tool back to "all platforms" without clearing `platform_ids` leaves the
 *    restriction in force while the form reads "All platforms".
 *
 * `fireEvent` rather than `user-event`, matching the rest of this suite.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as catalogService from "../services/catalogService";
import { ActiveClusterContext, type ActiveClusterState } from "../context/ActiveClusterContext";
import type { CatalogVocab, Platform, Tool } from "../types/catalog";
import { ToolRegistryView } from "./ToolRegistryView";

vi.mock("../services/catalogService");

const platform = (id: string, name: string, capabilities: string[] = []): Platform =>
  ({ id, name, capabilities, status: "ga" }) as unknown as Platform;

const tool = (over: Partial<Tool> = {}): Tool =>
  ({
    id: "nvloom",
    name: "NVLoom",
    category: "validation",
    script: "nvloom.sh",
    env_key: "",
    params: [],
    compatible: { match: "all", capabilities: [], platform_ids: [] },
    surfaces: ["tools"],
    execution: { mode: "async", target: "rack" },
    description: "",
    ai_context: "",
    status: "ga",
    source: "seed",
    ...over,
  }) as unknown as Tool;

const VOCAB = {
  ok: true,
  gpu_models: [],
  cpu_vendors: [],
  system_vendors: [],
  form_factors: [],
  cluster_managers: [],
  schedulers: [],
  tool_categories: ["validation"],
  exec_modes: ["async"],
  exec_targets: ["rack"],
  statuses: ["ga"],
} as CatalogVocab;

const PLATFORMS = [
  platform("gb200-nvl72", "GB200 NVL72", ["nvlink", "nvswitch", "imex"]),
  platform("gb300-nvl72", "GB300 NVL72", ["nvlink", "mnnvl"]),
];

/**
 * The view announces its writes through the active-cluster context, so the tests supply one.
 *
 * A stub rather than a real `ActiveClusterProvider`: the provider fetches the active context on
 * mount, and this file's subject is the compatibility editor, not catalog loading. `spyOn` on
 * `catalogChanged` is what lets a test assert the announcement happened.
 */
const clusterStub = (): ActiveClusterState => ({
  projects: [],
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

let cluster: ActiveClusterState;

function renderView() {
  cluster = clusterStub();
  return render(
    <ActiveClusterContext.Provider value={cluster}>
      <ToolRegistryView />
    </ActiveClusterContext.Provider>,
  );
}

/** Open the editor for the one listed tool and wait for the catalog to have loaded. */
async function openEditor(t: Tool = tool()) {
  vi.mocked(catalogService.listTools).mockResolvedValue([t]);
  vi.mocked(catalogService.getVocab).mockResolvedValue(VOCAB);
  vi.mocked(catalogService.listPlatforms).mockResolvedValue(PLATFORMS);
  renderView();
  const edit = await screen.findByRole("button", { name: /Edit NVLoom|Edit/ });
  fireEvent.click(edit);
  await waitFor(() => expect(screen.getByText("Platform compatibility")).toBeTruthy());
}

/**
 * Pick an option in a KUI Select or Combobox, by the control's accessible name.
 *
 * Same idiom as `MultirackView.test.tsx`, and for the same reason: the listbox lives in a
 * `popover="manual"` element, so it is outside the accessibility tree until opened and a plain
 * `getByRole("option")` cannot see it — hence `hidden: true`, scoped to the listbox this control
 * owns so two comboboxes on one form cannot answer for each other. Clicking still drives
 * `onValueChange`.
 */
function choose(controlLabel: string | RegExp, optionText: string | RegExp) {
  const control = screen.getByRole("combobox", { name: controlLabel });
  const listboxId = control.getAttribute("aria-controls");
  const options = screen
    .getAllByRole("option", { hidden: true })
    .filter((o) => (listboxId ? o.closest(`#${listboxId}`) !== null : true));
  const match = options.find((o) =>
    typeof optionText === "string" ? o.textContent === optionText : optionText.test(o.textContent ?? ""),
  );
  if (!match) {
    throw new Error(
      `option ${String(optionText)} not in ${String(controlLabel)}; saw ` +
        JSON.stringify(options.map((o) => o.textContent)),
    );
  }
  fireEvent.click(match);
}

/**
 * The names spelled out under the selector.
 *
 * Read off the tags rather than with `getByText`, because a closed listbox still has an
 * `<option>` for every choice — so "imex" matches twice and the assertion is ambiguous about
 * which one it found.
 */
function summaryTags(): string[] {
  return [...document.querySelectorAll(".nv-tag")].map((t) => t.textContent ?? "");
}

/** Options a control offers, for the "what can I pick?" assertions. */
function optionsOf(controlLabel: string | RegExp): string[] {
  const control = screen.getByRole("combobox", { name: controlLabel });
  const listboxId = control.getAttribute("aria-controls");
  return screen
    .getAllByRole("option", { hidden: true })
    .filter((o) => (listboxId ? o.closest(`#${listboxId}`) !== null : true))
    .map((o) => o.textContent ?? "");
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(catalogService.saveTool).mockResolvedValue(tool());
});

describe("scoping a tool to platforms", () => {
  it("offers the catalog's platforms, not just all-or-nothing", async () => {
    await openEditor();
    // The two modes that existed, plus the two the resolver always supported.
    expect(optionsOf(/Match/i)).toEqual([
      "All platforms",
      "Only selected platforms",
      "Platforms with capabilities",
      "None (disabled)",
    ]);
  });

  it("lists every platform by name once that mode is chosen", async () => {
    await openEditor();
    // Nothing to select from until the mode asks for it — an input the resolver ignores is an
    // invitation to fill in a field that does nothing.
    expect(screen.queryByRole("combobox", { name: "Platforms" })).toBeNull();

    choose(/Match/i, "Only selected platforms");
    expect(optionsOf("Platforms")).toEqual([
      "GB200 NVL72 (gb200-nvl72)",
      "GB300 NVL72 (gb300-nvl72)",
    ]);
  });

  it("saves the selected platform ids and clears the other two fields", async () => {
    await openEditor();
    choose(/Match/i, "Only selected platforms");
    choose("Platforms", /GB300 NVL72/);

    // The names are spelled out, because the trigger only says "1 platform".
    expect(summaryTags()).toContain("GB300 NVL72");

    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(catalogService.saveTool).toHaveBeenCalled());
    expect(vi.mocked(catalogService.saveTool).mock.calls[0][0].compatible).toEqual({
      match: "all",
      capabilities: [],
      platform_ids: ["gb300-nvl72"],
    });
  });

  it("refuses to save a scoped tool with nothing selected", async () => {
    // The silent failure: an empty platform_ids falls through to `match`, so the tool would be
    // offered on every platform — the exact opposite of "only selected platforms".
    await openEditor();
    choose(/Match/i, "Only selected platforms");
    expect(screen.getByText(/Select at least one platform/)).toBeTruthy();

    const save = screen.getByRole("button", { name: /^Save$/ }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.click(save);
    expect(catalogService.saveTool).not.toHaveBeenCalled();
  });

  it("clears a previous restriction when the tool goes back to all platforms", async () => {
    // Otherwise the form says "All platforms" while a non-empty platform_ids still decides, and
    // the tool keeps appearing on exactly one cluster.
    await openEditor(
      tool({ compatible: { match: "all", capabilities: [], platform_ids: ["gb200-nvl72"] } }),
    );
    expect(summaryTags()).toContain("GB200 NVL72");

    choose(/Match/i, "All platforms");
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));
    await waitFor(() => expect(catalogService.saveTool).toHaveBeenCalled());
    expect(vi.mocked(catalogService.saveTool).mock.calls[0][0].compatible).toEqual({
      match: "all",
      capabilities: [],
      platform_ids: [],
    });
  });

  it("opens an existing tool in the mode the resolver would use", async () => {
    // Same precedence as ToolCompatibility.matches: platform_ids, then capabilities, then match.
    await openEditor(
      tool({ compatible: { match: "all", capabilities: ["imex"], platform_ids: [] } }),
    );
    expect(screen.getByRole("combobox", { name: "Required capabilities" })).toBeTruthy();
    expect(summaryTags()).toContain("imex");
  });

  it("offers capabilities gathered from the platforms that declare them", async () => {
    // Not a hard-coded list: /api/catalog/vocab has no capability vocabulary, and a second list
    // here would make a capability an operator adds to a platform unofferable to a tool.
    await openEditor();
    choose(/Match/i, "Platforms with capabilities");
    expect(optionsOf("Required capabilities")).toEqual([
      "imex",
      "mnnvl",
      "nvlink",
      "nvswitch",
    ]);
  });
});

describe("keeping the boards in step", () => {
  it("announces a saved tool so the action boards refetch", async () => {
    /**
     * The bug this pins: a tool registered for the active platform did not appear in "Add
     * action" until a full page reload.
     *
     * `useBoardActions` fetches on mount and refetches on the context's `version`, which bumped
     * only when the active cluster changed — so nothing told the board its list was stale.
     * Registering a tool is precisely the moment it becomes stale.
     */
    await openEditor();
    vi.mocked(catalogService.saveTool).mockResolvedValue(tool());
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(vi.mocked(catalogService.saveTool)).toHaveBeenCalled());
    await waitFor(() => expect(cluster.catalogChanged).toHaveBeenCalled());
  });

  it("announces a deleted tool too", async () => {
    // Otherwise a board keeps offering an action whose launch now 404s.
    vi.mocked(catalogService.deleteTool).mockResolvedValue(undefined as never);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(catalogService.listTools).mockResolvedValue([tool()]);
    vi.mocked(catalogService.getVocab).mockResolvedValue(VOCAB);
    vi.mocked(catalogService.listPlatforms).mockResolvedValue(PLATFORMS);
    renderView();

    fireEvent.click(await screen.findByRole("button", { name: /Delete/ }));
    await waitFor(() => expect(cluster.catalogChanged).toHaveBeenCalled());
  });
});
