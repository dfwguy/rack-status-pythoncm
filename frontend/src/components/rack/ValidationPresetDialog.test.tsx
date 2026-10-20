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
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ValidationPresetDialog } from "./ValidationPresetDialog";
import type { ValidationPresetPrepareResponse, ValidationPresetsResponse } from "../../lib/validationPresets";

function jsonResponse(body: object, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

const catalog: ValidationPresetsResponse = {
  ok: true,
  presets: [
    {
      id: "nccl-2.31.2",
      label: "NCCL 2.31.2 / CUDA 13.3+",
      description: "Direct-host acceptance.",
      kind: "nccl",
      defaults: { storage: "nfs" },
      requirements: { scopes: ["node", "rack", "multirack"] },
    },
    {
      id: "llama31-405b-fp8",
      label: "Llama 3.1 405B FP8 — 50 steps",
      description: "Proven full validation.",
      kind: "llama405b",
      defaults: { storage: "local", rsync: true },
      requirements: { scopes: ["multirack"], container_profile: "26.04.01-nccl2.30.7" },
    },
    {
      id: "node-gate",
      label: "Node gate — cleanup + health check between jobs",
      description: "Cleanup and health check between jobs.",
      kind: "node_gate",
      defaults: { storage: "nfs", rsync: false, container_profile: null, max_runtime_minutes: 20 },
      requirements: { scopes: ["node", "rack", "multirack"] },
    },
    {
      id: "exemplar-system-info",
      label: "NVIDIA DGXC v26.06.01 system_info",
      description: "Validate system inventory.",
      kind: "exemplar_sysinfo",
      defaults: { storage: "local", rsync: false, local_container_dir: "/opt/containers" },
      requirements: { scopes: ["node", "rack", "multirack"] },
    },
  ],
};

afterEach(() => vi.restoreAllMocks());

describe("ValidationPresetDialog", () => {
  it("does not offer container staging for the direct-host NCCL preset", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementationOnce(() => jsonResponse(catalog));
    render(
      <ValidationPresetDialog
        open
        onClose={() => undefined}
        presetId="nccl-2.31.2"
        scope="nodes"
        nodes={["d01-01"]}
        onLaunch={() => undefined}
      />,
    );

    expect(screen.getByText(/fixed direct-host CUDA 13.3\+ validation/i)).toBeInTheDocument();
    expect(screen.queryByText("Container storage")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /Rsync container/i })).not.toBeInTheDocument();
  });

  it("warns that the node gate kills leftover GPU processes and offers no container staging", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementationOnce(() => jsonResponse(catalog));
    render(
      <ValidationPresetDialog
        open
        onClose={() => undefined}
        presetId="node-gate"
        scope="rack"
        racks={["U8"]}
        onLaunch={() => undefined}
      />,
    );

    expect(await screen.findByText(/kills any process still on their GPUs/i)).toBeInTheDocument();
    expect(screen.queryByText("Container storage")).not.toBeInTheDocument();
  });

  it("still offers container storage for a recipe the catalog has not described yet", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementationOnce(() => jsonResponse(catalog));
    render(
      <ValidationPresetDialog
        open
        onClose={() => undefined}
        presetId="exemplar-system-info"
        scope="nodes"
        nodes={["d01-01"]}
        onLaunch={() => undefined}
      />,
    );

    expect(await screen.findByText("Container storage")).toBeInTheDocument();
  });

  it("keeps Llama fixed at 50 steps and launches only after a reviewed dry-run", async () => {
    const plan: ValidationPresetPrepareResponse = {
      ok: true,
      plan: {
        preset_id: "llama31-405b-fp8",
        plan_hash: "reviewed-llama-plan",
        summary: "Run the proven 50-step Llama recipe on 256 nodes / 1024 GPUs.",
        scope: "multirack",
        racks: Array.from({ length: 16 }, (_, index) => `rack-${String(index + 1).padStart(2, "0")}`),
        node_count: 256,
        max_steps: 50,
        container_profile: "26.04.01-nccl2.30.7",
        checks: ["local image present", "/dev/infiniband mounted"],
        command_display: "sbatch --nodes=2 llama405b-fp8-50.sbatch",
      },
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => jsonResponse(catalog))
      .mockImplementationOnce(() => jsonResponse(plan));
    const onLaunch = vi.fn().mockResolvedValue(undefined);

    render(
      <ValidationPresetDialog
        open
        onClose={() => undefined}
        presetId="llama31-405b-fp8"
        scope="multirack"
        racks={Array.from({ length: 16 }, (_, index) => `rack-${String(index + 1).padStart(2, "0")}`)}
        logDir="/cluster/logs"
        campaignId="exemplar-16"
        onLaunch={onLaunch}
      />,
    );

    expect(screen.getByText(/exactly 256 nodes \/ 1024 GB300 GPUs/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Launch validation" })).toBeDisabled();
    await waitFor(() => expect(screen.getByRole("button", { name: "Review dry run" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Review dry run" }));

    expect(await screen.findByText("Run the proven 50-step Llama recipe on 256 nodes / 1024 GPUs.")).toBeInTheDocument();
    expect(screen.getByText("50 steps")).toBeInTheDocument();
    expect(screen.getByText("/dev/infiniband mounted", { exact: false })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/validation-presets/prepare",
      expect.objectContaining({ method: "POST" }),
    );

    const prepareBody = JSON.parse(String((fetchMock.mock.calls[1]?.[1] as RequestInit).body));
    expect(prepareBody).toMatchObject({
      preset_id: "llama31-405b-fp8",
      storage: "local",
      rsync_to_local: true,
      campaign: "exemplar-16",
    });
    expect(prepareBody).not.toHaveProperty("max_steps");

    fireEvent.click(screen.getByRole("button", { name: "Launch validation" }));
    await waitFor(() => expect(onLaunch).toHaveBeenCalledWith(
      expect.objectContaining({ preset_id: "llama31-405b-fp8", plan_hash: "reviewed-llama-plan" }),
      expect.anything(),
    ));
  });

  it("forces node-local storage when rsync staging is selected", async () => {
    const plan: ValidationPresetPrepareResponse = {
      ok: true,
      plan: {
        preset_id: "exemplar-system-info",
        plan_hash: "reviewed-sysinfo-plan",
        summary: "Fan out DGXC system_info to every selected node.",
        scope: "multirack",
        racks: ["d01", "d02"],
        node_count: 32,
        storage: "local",
        rsync_to_local: true,
        local_container_dir: "/tmp/containers",
        checks: ["rsync missing images only"],
        command_display: "sysinfo --recipe v26.06.01 d01 d02",
      },
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementationOnce(() => jsonResponse(catalog))
      .mockImplementationOnce(() => jsonResponse(plan));

    render(
      <ValidationPresetDialog
        open
        onClose={() => undefined}
        presetId="exemplar-system-info"
        scope="multirack"
        racks={["d02", "d01"]}
        onLaunch={() => undefined}
      />,
    );

    expect(screen.getByText(/fans out to every selected node/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Review dry run" })).toBeEnabled());
    fireEvent.click(screen.getByRole("checkbox", { name: /Rsync container before validation/i }));
    fireEvent.click(screen.getByRole("button", { name: "Review dry run" }));
    expect(await screen.findByText("Fan out DGXC system_info to every selected node.")).toBeInTheDocument();

    const request = JSON.parse(String((fetchMock.mock.calls[1]?.[1] as RequestInit).body));
    expect(request).toMatchObject({
      racks: ["d01", "d02"],
      storage: "local",
      rsync_to_local: true,
    });
    // The node cache is the cluster's, reported by the server: shown, never sent back.
    expect(request).not.toHaveProperty("local_container_dir");
    expect(screen.getByText(/rsync only missing\/stale content into \/opt\/containers/i)).toBeInTheDocument();
  });

  it("verifies instead of offering rsync when P2P distributes the cluster's images", async () => {
    const p2pCatalog: ValidationPresetsResponse = {
      ok: true,
      presets: [{
        id: "exemplar-system-info",
        label: "NVIDIA DGXC v26.06.01 system_info",
        description: "Validate system inventory.",
        kind: "exemplar_sysinfo",
        defaults: { storage: "local", rsync: false, local_container_dir: "/opt/containers", p2p_managed: true },
        requirements: { scopes: ["node", "rack", "multirack"] },
      }],
    };
    vi.spyOn(globalThis, "fetch").mockImplementationOnce(() => jsonResponse(p2pCatalog));
    render(
      <ValidationPresetDialog
        open
        onClose={() => undefined}
        presetId="exemplar-system-info"
        scope="multirack"
        racks={["d01"]}
        onLaunch={() => undefined}
      />,
    );

    expect(await screen.findByText(/NVIS-P2P distributes images on this cluster/i)).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /Rsync container/i })).not.toBeInTheDocument();
  });

  it("blocks prepare when the server catalog does not support the selected scope", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementationOnce(() =>
      jsonResponse({
        ok: true,
        presets: [{
          id: "mnubergemm-26.03-v1.17",
          label: "MNUberGEMM 26.03-v1.17",
          description: "Pinned image.",
          kind: "mnubergemm",
          requirements: { scopes: ["node", "rack"] },
        }],
      }),
    );

    render(
      <ValidationPresetDialog
        open
        onClose={() => undefined}
        presetId="mnubergemm-26.03-v1.17"
        scope="multirack"
        racks={["d01", "d02"]}
        onLaunch={() => undefined}
      />,
    );

    expect(await screen.findByText(/supports node, rack scope, not multirack/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review dry run" })).toBeDisabled();
  });
});
