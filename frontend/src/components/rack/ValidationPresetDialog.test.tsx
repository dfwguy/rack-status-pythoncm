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
      id: "nccl-2.29.3",
      label: "NCCL 2.29.3 — NVFTP 8K–32G / 500 iterations",
      description: "Proven containerized acceptance.",
      kind: "nccl",
      defaults: { storage: "local", rsync: false },
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
      id: "exemplar-system-info",
      label: "NVIDIA DGXC v26.06.01 system_info",
      description: "Validate system inventory.",
      kind: "exemplar_sysinfo",
      defaults: { storage: "local", rsync: false },
      requirements: { scopes: ["node", "rack", "multirack"] },
    },
  ],
};

afterEach(() => vi.restoreAllMocks());

describe("ValidationPresetDialog", () => {
  it("pins the proven NCCL preset to the already staged local image", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementationOnce(() => jsonResponse(catalog));
    render(
      <ValidationPresetDialog
        open
        onClose={() => undefined}
        presetId="nccl-2.29.3"
        scope="nodes"
        nodes={["d01-01"]}
        onLaunch={() => undefined}
      />,
    );

    expect(screen.getByText(/NCCL 2.29.3 \/ CUDA 13.1/i)).toBeInTheDocument();
    expect(screen.queryByText("Container storage")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /Rsync container/i })).not.toBeInTheDocument();
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
        local_container_dir: "/opt/containers",
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
      local_container_dir: "/opt/containers",
    });
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

  it("keeps an unavailable preset discoverable while blocking prepare with exact remediation", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementationOnce(() =>
      jsonResponse({
        ok: true,
        presets: [{
          id: "gpt-oss-cpu",
          label: "GPT-OSS CPU microbenchmark",
          description: "One independent job per node.",
          kind: "gpt_oss",
          requirements: { scopes: ["node", "rack", "multirack"] },
          availability: {
            available: false,
            status: "blocked",
            reasons: [
              "missing required container image: /opt/containers/trtllm.sqsh",
              "missing required model directory: /models/gpt-oss",
            ],
          },
        }],
      }),
    );

    render(
      <ValidationPresetDialog
        open
        onClose={() => undefined}
        presetId="gpt-oss-cpu"
        scope="nodes"
        nodes={["d01-01"]}
        onLaunch={() => undefined}
      />,
    );

    expect(await screen.findByText(/This recipe is currently unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/missing required container image: \/opt\/containers\/trtllm.sqsh/i)).toBeInTheDocument();
    expect(screen.getByText(/missing required model directory: \/models\/gpt-oss/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review dry run" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Launch validation" })).toBeDisabled();
  });
});
