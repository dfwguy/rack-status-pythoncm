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
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Mounts the real board against react-grid-layout, for the reason TerminalGrid.test gives: v2
// moved v1's flat props into config objects, so code written against the older API can
// type-check and still silently lose (or keep) dragging. This board's whole contract is that
// cards do NOT resize and only drag in Customise mode, and only mounting proves it.
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeAll, describe, expect, it, vi } from "vitest";
import { ToolWidgetBoard } from "./ToolWidgetBoard";
import {
  WIDGET_CARD_HEIGHT,
  WIDGET_CARD_WIDTH,
  boardStorageKey,
  parseCustomization,
} from "./widgetLayout";
import { fromTool, fromWorkflow } from "./boardActions";
import type { Tool } from "../../types/catalog";
import type { BoardAction } from "./boardActions";

const SURFACE = "rackDetail.validation";
const CLUSTER = "nscale-tx-gb300";

beforeAll(() => {
  // react-grid-layout measures its container; jsdom has no ResizeObserver.
  if (!("ResizeObserver" in globalThis)) {
    (globalThis as Record<string, unknown>).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
});

function tool(id: string, over: Partial<Tool> = {}): Tool {
  return {
    id,
    name: id,
    category: "validation",
    script: `${id}.sh`,
    env_key: "",
    params: [],
    compatible: { match: "all", capabilities: [], platform_ids: [] },
    surfaces: [SURFACE],
    execution: { mode: "async", target: "rack" },
    description: `${id} description`,
    ai_context: "",
    status: "ga",
    source: "seed",
    subtitle: "",
    button_label: "Run",
    icon: "",
    image: "",
    destructive: false,
    order: 100,
    launch: {
      kind: "async-job",
      endpoint: `/api/run-${id}-async`,
      preset_id: "",
      dialog: "",
      scopes: [],
      body: {},
      confirm: false,
      min_nodes: 0,
      max_nodes: 0,
    },
    ...over,
  };
}

function renderBoard(tools: Tool[], onRun = vi.fn(), extra: BoardAction[] = []) {
  const actions = [...tools.map(fromTool), ...extra];
  const utils = render(
    <ToolWidgetBoard surface={SURFACE} actions={actions} clusterId={CLUSTER} onRun={onRun} />,
  );
  return { ...utils, onRun };
}

/** A workflow action, for the cases about flows rather than catalog tools. */
function workflow(id: string, over: Partial<Parameters<typeof fromWorkflow>[0]> = {}) {
  return fromWorkflow({
    id,
    name: id,
    description: `${id} description`,
    version: 1,
    step_count: 3,
    ...over,
  });
}

const stored = () => parseCustomization(localStorage.getItem(boardStorageKey(CLUSTER, SURFACE)));

describe("rendering the catalog's board", () => {
  it("renders a card per launchable tool on the surface", () => {
    renderBoard([tool("stage1", { order: 10 }), tool("stage2", { order: 20 })]);
    expect(screen.getByText("stage1")).toBeTruthy();
    expect(screen.getByText("stage2")).toBeTruthy();
  });

  it("shows the document's subtitle and button label, not a hard-coded one", () => {
    // The whole point of the feature: a version bump is a catalog edit, not a release.
    renderBoard([tool("nccl", { subtitle: "NCCL 2.31.2 · CUDA 13.3+", button_label: "Plan NCCL" })]);
    expect(screen.getByText("NCCL 2.31.2 · CUDA 13.3+")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Plan NCCL" })).toBeTruthy();
  });

  it("hands the whole tool to the launcher when pressed", () => {
    const { onRun } = renderBoard([tool("stage1")]);
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(onRun).toHaveBeenCalledTimes(1);
    expect(onRun.mock.calls[0][0].id).toBe("stage1");
  });

  it("disables a card whose binding this build cannot honour, and says why", () => {
    renderBoard([
      tool("broken", { launch: { ...tool("x").launch, endpoint: "https://evil.example/api/x" } }),
    ]);
    expect((screen.getByRole("button", { name: "Run" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(/endpoint is not ours/)).toBeTruthy();
  });

  it("explains an empty surface rather than rendering nothing", () => {
    renderBoard([tool("elsewhere", { surfaces: ["multirack"] })]);
    expect(screen.getByText(/No catalog tools are bound to this surface/)).toBeTruthy();
  });
});

describe("the card", () => {
  it("uses KUI Card's own slots: category badge, banner, and both buttons inside", () => {
    // Pins the structure the buttons kept disappearing from. Two earlier versions fought the
    // component -- one put `display: flex` on the root without realising `Card` wraps children
    // in a `.nv-card-content` grid, so every flex rule landed on the wrong element; the other
    // replaced Card with a hand-rolled div. Letting Card lay itself out via slotHeader and
    // slotMedia is what makes a fixed card size work, so the slots are the thing to guard.
    renderBoard([
      tool("nvloom", { button_label: "Run nvloom", category: "performance" }),
    ]);
    const card = document.querySelector(".nv-card-root") as HTMLElement;
    expect(card).toBeTruthy();
    expect(card.querySelector(".nv-card-content")).toBeTruthy();

    // The badge names the category, which is also what picks the banner artwork.
    expect(screen.getByText("performance")).toBeTruthy();

    const banner = card.querySelector("img") as HTMLImageElement;
    expect(banner).toBeTruthy();
    // Decorative: the badge and title carry the meaning.
    expect(banner.getAttribute("alt")).toBe("");

    // Both buttons live inside the card, not in some sibling the grid can clip away.
    for (const name of [/Info/, "Run nvloom"] as const) {
      expect(card.contains(screen.getByRole("button", { name }))).toBe(true);
    }
  });

  it("lets the browser size the columns, with no react-grid-layout in sight", () => {
    // The board used to hand react-grid-layout a measured pixel width in every mode, which
    // made it the only card grid in the app that could not reflow: a JS measurement decides
    // 1, 2 or 3 across, and a wrong measurement decides wrongly. Reading the board is now a
    // CSS grid, like ToolRegistryView and its neighbours, so there is no number to get wrong.
    renderBoard([tool("a"), tool("b"), tool("c")]);
    expect(document.querySelectorAll(".react-grid-item")).toHaveLength(0);
    const grid = document.querySelector(".nvis-widget-board") as HTMLElement;
    expect(grid.classList.contains("nv-grid")).toBe(true);
    expect(grid.getAttribute("style")).toContain(`minmax(${WIDGET_CARD_WIDTH}px`);
    expect(document.querySelectorAll(".nv-card-root")).toHaveLength(3);
  });

  it("mounts react-grid-layout only to drag, with a fixed footprint per card", () => {
    // Dragging is the one thing CSS cannot do, so the positioning engine is scoped to it.
    renderBoard([tool("a"), tool("b"), tool("c")]);
    fireEvent.click(screen.getByRole("button", { name: /Customize/ }));
    const items = [...document.querySelectorAll(".react-grid-item")] as HTMLElement[];
    expect(items).toHaveLength(3);
    for (const item of items) {
      expect(item.style.width).toBe(`${WIDGET_CARD_WIDTH}px`);
      expect(item.style.height).toBe(`${WIDGET_CARD_HEIGHT}px`);
    }
  });

  it("falls back to category artwork when the document names no image", () => {
    renderBoard([tool("x", { category: "networking", image: "" })]);
    const src = (document.querySelector(".nv-card-root img") as HTMLImageElement).src;
    expect(src).toMatch(/networking/);
  });

  it("honours a per-tool image override", () => {
    renderBoard([tool("x", { category: "networking", image: "/custom/banner.jpg" })]);
    const src = (document.querySelector(".nv-card-root img") as HTMLImageElement).src;
    expect(src).toContain("/custom/banner.jpg");
  });
});

describe("workflows on a board", () => {
  it("is offered under Add action but never on the default board", () => {
    // A flow is one operator's work in progress; the catalog's tools are curated and shipped.
    // Appearing unasked on everyone's rack board would be a surprise, possibly a destructive
    // one, so a workflow carries no surfaces and arrives only when chosen.
    renderBoard([tool("stage1")], vi.fn(), [workflow("wf1", { name: "Bring-up flow" })]);
    expect(screen.queryByText("Bring-up flow")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Customize/ }));
    fireEvent.click(screen.getByRole("button", { name: /Add action/ }));
    fireEvent.click(screen.getByRole("button", { name: "Add Bring-up flow" }));
    expect(stored()?.added).toEqual(["workflow:wf1"]);
  });

  it("renders with its own badge and step count, and hands the action to the launcher", () => {
    const onRun = vi.fn();
    localStorage.setItem(
      boardStorageKey(CLUSTER, SURFACE),
      JSON.stringify({ v: 3, added: ["workflow:wf1"], removed: [], layout: [] }),
    );
    renderBoard([], onRun, [workflow("wf1", { name: "Bring-up flow", step_count: 5, version: 2 })]);

    expect(screen.getByText("Bring-up flow")).toBeTruthy();
    expect(screen.getByText("workflow")).toBeTruthy();
    expect(screen.getByText("5 steps · v2")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Run flow" }));
    expect(onRun).toHaveBeenCalledTimes(1);
    expect(onRun.mock.calls[0][0]).toMatchObject({ kind: "workflow", id: "wf1" });
  });

  it("a tool id and a workflow id of the same name do not collide", () => {
    // The reason keys are namespaced: the two id spaces are independent, and a stored layout
    // must not silently re-point at the other one.
    localStorage.setItem(
      boardStorageKey(CLUSTER, SURFACE),
      JSON.stringify({ v: 3, added: ["workflow:clusterkit"], removed: [], layout: [] }),
    );
    renderBoard([tool("clusterkit", { name: "ClusterKit tool" })], vi.fn(), [
      workflow("clusterkit", { name: "ClusterKit flow" }),
    ]);
    expect(screen.getByText("ClusterKit tool")).toBeTruthy();
    expect(screen.getByText("ClusterKit flow")).toBeTruthy();
  });

  it("shows workflow details rather than tool-shaped rows", () => {
    localStorage.setItem(
      boardStorageKey(CLUSTER, SURFACE),
      JSON.stringify({ v: 3, added: ["workflow:wf1"], removed: [], layout: [] }),
    );
    renderBoard([], vi.fn(), [workflow("wf1", { name: "Bring-up flow" })]);
    fireEvent.click(screen.getByRole("button", { name: /Info/ }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/\/api\/workflows\/wf1\/run/)).toBeTruthy();
    // No script row: a workflow has no catalog document behind it.
    expect(within(dialog).queryByText("Script")).toBeNull();
  });
});

describe("the details modal", () => {
  it("opens from the card's Info button with the full document", () => {
    // The card shows one ellipsised line; this is where the rest has to be reachable, or
    // truncating it would just be losing it.
    renderBoard([
      tool("nccl", {
        name: "NCCL Acceptance Sweep",
        subtitle: "NCCL 2.31.2 · CUDA 13.3+",
        description:
          "Direct host NCCL acceptance sweep. Fails closed unless the selected runtime really is CUDA 13.3+ with NCCL 2.31.2.",
        script: "run-validation-preset.sh",
      }),
    ]);
    fireEvent.click(screen.getByRole("button", { name: /Info/ }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toBeTruthy();
    expect(within(dialog).getByText(/Fails closed unless/)).toBeTruthy();
    expect(within(dialog).getByText("run-validation-preset.sh")).toBeTruthy();
  });

  it("names the endpoint and its poll route, so a stuck run can be traced", () => {
    renderBoard([tool("nvloom")]);
    fireEvent.click(screen.getByRole("button", { name: /Info/ }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/\/api\/run-nvloom-async/)).toBeTruthy();
    expect(within(dialog).getByText(/run-nvloom-status/)).toBeTruthy();
  });

  it("stays available for an action that cannot be launched", () => {
    // Reading about an action is never blocked: an unlaunchable card is exactly the one an
    // operator needs to inspect.
    renderBoard([
      tool("broken", { launch: { ...tool("x").launch, endpoint: "https://evil.example/api/x" } }),
    ]);
    const infoButton = screen.getByRole("button", { name: /Info/ }) as HTMLButtonElement;
    expect(infoButton.disabled).toBe(false);
    fireEvent.click(infoButton);
    // Twice by design: once as the warning banner, once as the "How it runs" detail.
    expect(
      within(screen.getByRole("dialog")).getAllByText(/endpoint is not ours/).length,
    ).toBeGreaterThan(0);
  });

  it("closes again", () => {
    renderBoard([tool("nvloom")]);
    fireEvent.click(screen.getByRole("button", { name: /Info/ }));
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("customise mode", () => {
  it("is off by default: no grips, no remove controls", () => {
    renderBoard([tool("stage1")]);
    expect(screen.queryByLabelText("Move stage1")).toBeNull();
    expect(screen.queryByLabelText("Remove stage1")).toBeNull();
  });

  it("reveals the grip and remove control when turned on", () => {
    renderBoard([tool("stage1")]);
    fireEvent.click(screen.getByRole("button", { name: /Customize/ }));
    expect(screen.getByLabelText("Move stage1")).toBeTruthy();
    expect(screen.getByLabelText("Remove stage1")).toBeTruthy();
  });

  it("removes a widget and remembers it", () => {
    renderBoard([tool("stage1", { order: 10 }), tool("stage2", { order: 20 })]);
    fireEvent.click(screen.getByRole("button", { name: /Customize/ }));
    fireEvent.click(screen.getByLabelText("Remove stage1"));
    expect(screen.queryByText("stage1")).toBeNull();
    expect(screen.getByText("stage2")).toBeTruthy();
    expect(stored()?.removed).toEqual(["tool:stage1"]);
  });

  it("adds a tool from another surface", () => {
    renderBoard([tool("stage1"), tool("multirack-thing", { surfaces: ["multirack"] })]);
    fireEvent.click(screen.getByRole("button", { name: /Customize/ }));
    fireEvent.click(screen.getByRole("button", { name: /Add action/ }));
    fireEvent.click(screen.getByRole("button", { name: "Add multirack-thing" }));
    expect(stored()?.added).toEqual(["tool:multirack-thing"]);
    expect(screen.getAllByText("multirack-thing").length).toBeGreaterThan(0);
  });

  it("offers Export and Import only in customise mode", () => {
    renderBoard([tool("stage1")]);
    expect(screen.queryByRole("button", { name: /Export/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Import/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Customize/ }));
    expect(screen.getByRole("button", { name: /Export/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Import/ })).toBeTruthy();
  });

  it("cannot export an empty board, but can still import into one", () => {
    renderBoard([tool("only", { surfaces: ["multirack"] })]);
    fireEvent.click(screen.getByRole("button", { name: /Customize/ }));
    expect((screen.getByRole("button", { name: /Export/ }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect((screen.getByRole("button", { name: /Import/ }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  it("offers Reset only once something has been changed", () => {
    renderBoard([tool("stage1")]);
    fireEvent.click(screen.getByRole("button", { name: /Customize/ }));
    expect(screen.queryByRole("button", { name: /Reset to default/ })).toBeNull();
    fireEvent.click(screen.getByLabelText("Remove stage1"));
    expect(screen.getByRole("button", { name: /Reset to default/ })).toBeTruthy();
  });

  it("Reset clears the stored record rather than writing an empty one", () => {
    // Storing "no changes" would be indistinguishable from a customisation, and a board reset
    // today must still pick up a tool the catalog gains tomorrow.
    renderBoard([tool("stage1")]);
    fireEvent.click(screen.getByRole("button", { name: /Customize/ }));
    fireEvent.click(screen.getByLabelText("Remove stage1"));
    expect(localStorage.getItem(boardStorageKey(CLUSTER, SURFACE))).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Reset to default/ }));
    expect(localStorage.getItem(boardStorageKey(CLUSTER, SURFACE))).toBeNull();
    expect(screen.getByText("stage1")).toBeTruthy();
  });
});

describe("persistence", () => {
  it("restores a stored customisation on mount", () => {
    localStorage.setItem(
      boardStorageKey(CLUSTER, SURFACE),
      JSON.stringify({ v: 3, added: [], removed: ["tool:stage1"], layout: [] }),
    );
    renderBoard([tool("stage1", { order: 10 }), tool("stage2", { order: 20 })]);
    expect(screen.queryByText("stage1")).toBeNull();
    expect(screen.getByText("stage2")).toBeTruthy();
  });

  it("migrates a v2 record instead of discarding it", () => {
    // v2 stored bare tool ids, because a board could not hold anything else. Prefixing them
    // is lossless, so an operator who customised before workflows arrived keeps their board
    // rather than being reset a second time in a week.
    localStorage.setItem(
      boardStorageKey(CLUSTER, SURFACE),
      JSON.stringify({ v: 2, added: [], removed: ["stage1"], layout: [] }),
    );
    renderBoard([tool("stage1", { order: 10 }), tool("stage2", { order: 20 })]);
    expect(screen.queryByText("stage1")).toBeNull();
    expect(screen.getByText("stage2")).toBeTruthy();
  });

  it("keeps boards on different clusters apart", () => {
    localStorage.setItem(
      boardStorageKey("some-other-cluster", SURFACE),
      JSON.stringify({ v: 3, added: [], removed: ["tool:stage1"], layout: [] }),
    );
    renderBoard([tool("stage1")]);
    expect(screen.getByText("stage1")).toBeTruthy();
  });

  it("does not write to storage just by rendering, or by opening Customise", () => {
    // react-grid-layout emits onLayoutChange on mount, so entering Customise is a mount that
    // looks exactly like a drag. Persisting it would make every board that was ever *looked*
    // at appear customised — and would offer Reset for a change nobody made.
    renderBoard([tool("stage1"), tool("stage2")]);
    expect(localStorage.getItem(boardStorageKey(CLUSTER, SURFACE))).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Customize/ }));
    expect(localStorage.getItem(boardStorageKey(CLUSTER, SURFACE))).toBeNull();
  });

  it("reads a v3 record, taking its positions as the order the operator meant", () => {
    // v3 stored x/y. A browser-sized grid has no coordinate space to put them back into, so
    // they are read top-row-first — which is what an operator arranging cards was saying.
    // Discarding them instead would have reset every customised board in the field.
    localStorage.setItem(
      boardStorageKey(CLUSTER, SURFACE),
      JSON.stringify({
        v: 3,
        added: [],
        removed: [],
        layout: [
          { i: "tool:stage2", x: 0, y: 0, w: 1, h: 1 },
          { i: "tool:stage1", x: 1, y: 0, w: 1, h: 1 },
        ],
      }),
    );
    renderBoard([tool("stage1", { order: 10 }), tool("stage2", { order: 20 })]);
    expect(stored()).toEqual({ v: 4, added: [], removed: [], order: ["tool:stage2", "tool:stage1"] });
    // stage2 renders first, against its catalog order of 20 — the stored arrangement wins.
    const cards = [...document.querySelectorAll(".nv-card-root")];
    expect(cards).toHaveLength(2);
    expect(cards[0].textContent).toContain("stage2");
    expect(cards[1].textContent).toContain("stage1");
  });
});
