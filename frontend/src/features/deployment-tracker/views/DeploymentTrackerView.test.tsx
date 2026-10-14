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
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { REPORT } from "../__fixtures__/report";
import { DeploymentTrackerView } from "./DeploymentTrackerView";

const service = vi.hoisted(() => ({
  fetchDeploymentTracker: vi.fn(),
  downloadTrackerSite: vi.fn(),
}));
vi.mock("../services/trackerService", () => service);

afterEach(() => {
  vi.clearAllMocks();
});

describe("DeploymentTrackerView", () => {
  it("loads the dataset and titles the page with the cluster", async () => {
    service.fetchDeploymentTracker.mockResolvedValue({ ok: true, ...REPORT });
    render(<DeploymentTrackerView />);
    expect(await screen.findByText("Nscale NC · Deployment tracker")).toBeInTheDocument();
    expect(screen.getByTestId("kpi-total")).toHaveTextContent("5");
  });

  it("says why when there is nothing to show yet", async () => {
    service.fetchDeploymentTracker.mockRejectedValue(new Error("node inventory is still loading; retry shortly"));
    render(<DeploymentTrackerView />);
    expect(await screen.findByText("node inventory is still loading; retry shortly")).toBeInTheDocument();
  });

  it("keeps the last good data when a refresh fails, and exports the static page", async () => {
    service.fetchDeploymentTracker.mockResolvedValueOnce({ ok: true, ...REPORT });
    service.downloadTrackerSite.mockResolvedValue(undefined);
    render(<DeploymentTrackerView />);
    await screen.findByTestId("kpi-total");

    service.fetchDeploymentTracker.mockRejectedValueOnce(new Error("HTTP 502"));
    fireEvent.click(screen.getByRole("button", { name: /Refresh/ }));
    expect(await screen.findByText("Showing the last good data: HTTP 502")).toBeInTheDocument();
    expect(screen.getByTestId("kpi-total")).toHaveTextContent("5");

    fireEvent.click(screen.getByRole("button", { name: /Download static page/ }));
    await waitFor(() => expect(service.downloadTrackerSite).toHaveBeenCalledTimes(1));
  });
});
