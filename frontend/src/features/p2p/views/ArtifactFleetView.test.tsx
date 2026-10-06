/*
 * SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { FleetStatus } from "../types/fleet";
import ArtifactFleetView from "./ArtifactFleetView";

const fleetHook = vi.hoisted(() => ({
  value: {
    status: null as FleetStatus | null,
    error: null as string | null,
    loading: false,
    unauthenticated: false,
    refresh: vi.fn().mockResolvedValue(undefined),
  },
}));
vi.mock("../hooks/useFleetStatus", () => ({ useFleetStatus: () => fleetHook.value }));

const settings = vi.hoisted(() => ({ provision: vi.fn() }));
vi.mock("../services/settingsService", () => settings);
vi.mock("../services/fleetService", () => ({ startOperation: vi.fn() }));

function status(provisioned: boolean): FleetStatus {
  return {
    provisioned,
    catalog: { canonical_directory: "/d", images: [] },
    object_store_healthy: false,
    fleet: [],
    coverage: { generated_at: null, required_count: 0, required_images: [], by_su: [], nodes: [] },
    sync: null,
    catalog_exact_duplicates: [],
    preflight_generated_at: null,
    operations: [],
  };
}

function renderView() {
  return render(
    <MemoryRouter>
      <ArtifactFleetView />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("ArtifactFleetView provisioning gate", () => {
  it("shows the provision gate and disables operations on a bare cluster", () => {
    fleetHook.value.status = status(false);
    renderView();
    expect(screen.getByText(/not provisioned yet/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Provision P2P instance/i })).toBeInTheDocument();
    // An operation button is disabled until the instance exists.
    expect(screen.getByRole("button", { name: /Sync catalog/i })).toBeDisabled();
  });

  it("starts provisioning when the operator clicks Provision", () => {
    settings.provision.mockResolvedValue({ ok: true, run_id: "run-123", poll: "/api/workflow-runs/run-123" });
    fleetHook.value.status = status(false);
    renderView();
    fireEvent.click(screen.getByRole("button", { name: /Provision P2P instance/i }));
    expect(settings.provision).toHaveBeenCalledTimes(1);
  });

  it("hides the gate and enables operations once provisioned", () => {
    fleetHook.value.status = status(true);
    renderView();
    expect(screen.queryByText(/not provisioned yet/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Sync catalog/i })).toBeEnabled();
  });
});
