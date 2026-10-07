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
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OperationLogDialog } from "./OperationLogDialog";
import { fetchOperationLog } from "../services/fleetService";
import type { Operation } from "../types/fleet";

// vitest hoists vi.mock above the imports, so the mocked fetchOperationLog is what the component sees.
vi.mock("../services/fleetService", () => ({
  fetchOperationLog: vi.fn(),
}));

const mockLog = fetchOperationLog as unknown as ReturnType<typeof vi.fn>;

const operation: Operation = {
  id: "distribute-20261007T042514Z-3cd688",
  action: "distribute",
  sus: [],
  images: [],
  state: "running",
  started_at: "2026-10-07T04:25:14Z",
  log: "/nvis/api/p2p/operations/distribute-20261007T042514Z-3cd688/log",
};

afterEach(() => {
  vi.clearAllMocks();
});

describe("OperationLogDialog", () => {
  it("shows an explicit empty-state note when a (running) operation has no log output yet", async () => {
    // The regression: an empty .log collapsed the viewer to a thin white bar. The box must stay a
    // readable rectangle and say why it is blank.
    mockLog.mockResolvedValue({ ok: true, content: "" });
    render(<OperationLogDialog open onClose={() => {}} operation={operation} />);
    await waitFor(() => expect(screen.getByText("No log output yet.")).toBeInTheDocument());
  });

  it("renders log content and drops the empty-state note once output exists", async () => {
    mockLog.mockResolvedValue({ ok: true, content: "DISTRIBUTE_LAUNCHED label=fleet nodes=1764" });
    render(<OperationLogDialog open onClose={() => {}} operation={operation} />);
    await waitFor(() => expect(screen.getByText(/DISTRIBUTE_LAUNCHED/)).toBeInTheDocument());
    expect(screen.queryByText("No log output yet.")).not.toBeInTheDocument();
  });

  it("surfaces a worker error instead of a blank box", async () => {
    mockLog.mockResolvedValue({ ok: false, error: "No log available." });
    render(<OperationLogDialog open onClose={() => {}} operation={operation} />);
    await waitFor(() => expect(screen.getByText("No log available.")).toBeInTheDocument());
    expect(screen.queryByText("No log output yet.")).not.toBeInTheDocument();
  });
});
