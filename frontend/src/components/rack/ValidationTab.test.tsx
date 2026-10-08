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
// The tab's launch actions now come from the Tool catalog, so these assertions moved with
// them: they used to read strings out of hand-written ActionCards, and they now drive the
// widget board with catalog documents. The *properties* being pinned are unchanged, because
// they are operational rather than cosmetic — GPT-OSS stays single-node on this surface, and
// the cross-rack Llama profile stays discoverable from here even though it runs elsewhere.
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { ValidationTab } from "./ValidationTab";
import type { Tool } from "../../types/catalog";
import { fromTool } from "../widgets/boardActions";

vi.mock("../../context/RackCommandContext", () => ({
  useRackCommands: () => ({
    runAsyncValidationFamily: vi.fn(),
    launchAsyncValidationFamily: vi.fn(),
    runSyncJson: vi.fn(),
    openText: vi.fn(),
  }),
}));
vi.mock("./NvlinkPreflightPanel", () => ({
  NvlinkPreflightPanel: () => <div>NVLink readiness panel</div>,
}));
vi.mock("./ValidationActivitiesTable", () => ({ ValidationActivitiesTable: () => null }));
vi.mock("../../context/ActiveClusterContext", () => ({
  useActiveCluster: () => ({ activeCluster: { id: "test-cluster" }, version: 0 }),
}));

const { boardActionsMock } = vi.hoisted(() => ({ boardActionsMock: vi.fn() }));
vi.mock("../../hooks/useBoardActions", () => ({ useBoardActions: boardActionsMock }));

beforeAll(() => {
  if (!("ResizeObserver" in globalThis)) {
    (globalThis as Record<string, unknown>).ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
});

/** Mirrors the documents `backend/catalog/seed.py` puts on this surface. */
function preset(id: string, over: Partial<Tool> = {}): Tool {
  return {
    id,
    name: id,
    category: "performance",
    script: "run-validation-preset.sh",
    env_key: "RACK_VALIDATION_PRESET_SCRIPT",
    params: [],
    compatible: { match: "all", capabilities: [], platform_ids: [] },
    surfaces: ["rackDetail.validation"],
    execution: { mode: "async", target: "rack" },
    description: `${id} description`,
    ai_context: "",
    status: "ga",
    source: "seed",
    subtitle: "",
    button_label: "Plan",
    icon: "",
    image: "",
    destructive: false,
    order: 100,
    launch: {
      kind: "validation-preset",
      endpoint: "",
      preset_id: id,
      dialog: "",
      scopes: ["rack"],
      body: {},
      confirm: false,
      min_nodes: 0,
      max_nodes: 0,
    },
    ...over,
  };
}

const GPT_OSS_SINGLE_NODE = preset("preset-gpt-oss-node", {
  name: "GPT-OSS CPU Microbenchmark",
  subtitle: "Exactly one node",
  button_label: "Plan GPT-OSS",
  launch: {
    ...preset("x").launch,
    preset_id: "gpt-oss-cpu",
    scopes: ["node"],
    min_nodes: 1,
    max_nodes: 1,
  },
});

beforeEach(() => {
  boardActionsMock.mockReset();
  boardActionsMock.mockReturnValue({
    // Adapted through `fromTool`, the way the real hook builds them.
    actions: [
      GPT_OSS_SINGLE_NODE,
      preset("preset-nemo", { name: "NeMo Validation", subtitle: "NeMo 26.04.01 · 50 steps" }),
      preset("preset-mnubergemm", {
        name: "MNUberGEMM",
        subtitle: "26.03-v1.17 · 60-minute ceiling",
      }),
    ].map(fromTool),
    loading: false,
    error: null,
    workflowError: null,
    reload: vi.fn(),
  });
});

function renderTab(nodes: string[]) {
  return render(
    <ValidationTab rack="d01" selectedNodes={new Set(nodes)} disabled={false} />,
  );
}

describe("ValidationTab pinned recipes", () => {
  it("hides NVLink preflight when the cluster catalog excludes it", () => {
    renderTab(["d01-01"]);
    expect(screen.queryByText("NVLink readiness panel")).not.toBeInTheDocument();
    expect(screen.queryByText("Preflight")).not.toBeInTheDocument();
  });

  it("shows NVLink preflight only after a compatible catalog loads", () => {
    const compatibleActions = [fromTool(preset("nvlink-preflight", { surfaces: [] }))];
    const state = {
      actions: compatibleActions, loading: false, error: null, reload: vi.fn(),
    };
    boardActionsMock.mockReturnValue(state);
    const view = renderTab(["d01-01"]);
    expect(screen.getByText("NVLink readiness panel")).toBeInTheDocument();

    for (const next of [
      { ...state, loading: true },
      { ...state, error: "Catalog unavailable" },
      { ...state, actions: [] },
    ]) {
      boardActionsMock.mockReturnValue(next);
      view.rerender(<ValidationTab rack="d01" selectedNodes={new Set(["d01-01"])} disabled={false} />);
      expect(screen.queryByText("NVLink readiness panel")).not.toBeInTheDocument();
      expect(screen.queryByText("Preflight")).not.toBeInTheDocument();
    }
  });

  it("shows the pinned versions from the catalog, not from component source", () => {
    renderTab(["d01-01"]);
    expect(screen.getByText("NeMo 26.04.01 · 50 steps")).toBeInTheDocument();
    expect(screen.getByText("26.03-v1.17 · 60-minute ceiling")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Plan GPT-OSS" })).toBeEnabled();
  });

  it("keeps the cross-rack Llama profile discoverable from this tab", () => {
    // It runs from Multirack, but an operator standing on a rack still has to be told where.
    renderTab(["d01-01"]);
    expect(screen.getByText(/Llama 3\.1 405B FP8/)).toBeInTheDocument();
    expect(screen.getByText(/256-node \/ 1024-GPU/)).toBeInTheDocument();
  });

  it("does not permit the single-node GPT-OSS overhead benchmark on multiple nodes", () => {
    renderTab(["d01-01", "d01-02"]);
    expect(screen.getByRole("button", { name: "Plan GPT-OSS" })).toBeDisabled();
    expect(screen.getByText(/exactly 1 node/i)).toBeInTheDocument();
  });

  it("forwards catalog NCCL versions from the launch body into the options dialog", () => {
    // The backend injects `nccl_versions` into the tool's launch body (derived from the P2P
    // catalog); the tab must carry it through PendingLaunch into the dialog, whose default is then
    // the first (newest) catalog build rather than the component's hard-coded 2.30.7.
    const nccl = preset("llmb-nccl", {
      name: "LLMB NCCL",
      button_label: "Run NCCL",
      launch: {
        ...preset("x").launch,
        kind: "async-job",
        endpoint: "/api/run-llmb-nccl-async",
        preset_id: "",
        dialog: "launch-options",
        scopes: ["rack"],
        body: {
          show_nccl_version: true,
          nccl_versions: [
            { value: "2.32.3", label: "2.32.3 (latest)", order: 1 },
            { value: "2.30.7", label: "2.30.7 (validated)", order: 2 },
          ],
        },
      },
    });
    boardActionsMock.mockReturnValue({
      actions: [fromTool(nccl)],
      loading: false,
      error: null,
      workflowError: null,
      reload: vi.fn(),
    });
    renderTab(["d01-01"]);

    fireEvent.click(screen.getByRole("button", { name: "Run NCCL" }));

    expect(screen.getByRole("combobox", { name: /NCCL version/i })).toHaveTextContent(
      "2.32.3 (latest)",
    );
  });

  it("reports a catalog it could not load instead of showing an empty tab", () => {
    boardActionsMock.mockReturnValue({
      actions: [],
      loading: false,
      error: "503 Service Unavailable",
      workflowError: null,
      reload: vi.fn(),
    });
    renderTab(["d01-01"]);
    expect(screen.getByText(/Could not load the tool catalog/)).toBeInTheDocument();
  });
});
