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
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HardwareTab } from "./HardwareTab";

const { launchAsyncValidationFamily } = vi.hoisted(() => ({ launchAsyncValidationFamily: vi.fn() }));
vi.mock("../../context/RackCommandContext", () => ({
  useRackCommands: () => ({
    runAsyncReset: vi.fn(),
    launchAsyncValidationFamily,
  }),
}));

afterEach(() => vi.restoreAllMocks());

describe("HardwareTab Exemplar validation", () => {
  it("opens a DGXC v26.06.01 system_info plan for every selected node", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          ok: true,
          presets: [
            {
              id: "exemplar-system-info",
              label: "NVIDIA DGXC v26.06.01 system_info",
              description: "Validate every node against the Exemplar recipe.",
              kind: "exemplar_sysinfo",
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    render(
      <HardwareTab
        rack="d01"
        logDir="/cluster/logs"
        selectedNodes={new Set(["d01-01", "d01-02"])}
        disabled={false}
        validationScope="rack"
        onSelectPowerResetTargets={() => undefined}
      />,
    );

    expect(screen.getByText(/every one of the 2 selected node/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Plan Sysinfo" }));
    expect(await screen.findByRole("dialog", { name: "NVIDIA DGXC v26.06.01 system_info" })).toBeInTheDocument();
    expect(screen.getByText(/fans out to every selected node/i)).toBeInTheDocument();
  });
});
