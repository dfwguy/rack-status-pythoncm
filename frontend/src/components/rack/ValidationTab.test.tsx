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
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ValidationTab } from "./ValidationTab";

vi.mock("../../context/RackCommandContext", () => ({
  useRackCommands: () => ({
    runAsyncValidationFamily: vi.fn(),
    launchAsyncValidationFamily: vi.fn(),
    runSyncJson: vi.fn(),
    openText: vi.fn(),
  }),
}));
vi.mock("./NvlinkPreflightPanel", () => ({ NvlinkPreflightPanel: () => null }));
vi.mock("./ValidationActivitiesTable", () => ({ ValidationActivitiesTable: () => null }));

describe("ValidationTab hybrid provider boundary", () => {
  it("keeps validation fallbacks available", () => {
    render(
      <ValidationTab
        rack="d01"
        selectedNodes={new Set(["d01-01"])}
        disabled={false}
      />,
    );

    expect(screen.getByText("Llama 3.1 405B FP8 — 1024 GPUs / 50 Steps")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Multirack Only" })).toBeDisabled();
    expect(screen.getByText("NeMo 26.04.01 Validation")).toBeInTheDocument();
    expect(screen.getByText("MNUberGEMM 26.03-v1.17 Validation")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Plan GPT-OSS" })).toBeEnabled();
    expect(screen.queryByText("NCCL 2.29.3 — NVFTP 8K–32G / 500")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run NCCL" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Run nvloom" })).toBeEnabled();
  });

  it("does not permit the single-node GPT-OSS overhead benchmark on multiple nodes", () => {
    render(
      <ValidationTab
        rack="d01"
        selectedNodes={new Set(["d01-01", "d01-02"])}
        disabled={false}
      />,
    );

    expect(screen.getByRole("button", { name: "Plan GPT-OSS" })).toBeDisabled();
    expect(screen.getByText(/exactly one selected node/i)).toBeInTheDocument();
  });
});
