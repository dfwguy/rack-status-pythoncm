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
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { NodeSelector, type NodeInfo } from "./NodeSelector";

const nodes: NodeInfo[] = [
  { id: "sys1-gso1-p-phy-gpu19-su19", hostname: "sys1-gso1-p-phy-gpu19-su19", label: "U20-ct01", placement: "U20:11", status: "up", ip: "10.10.171.244" },
  { id: "p5-r01-ct01", status: "unknown" },
];

describe("NodeSelector", () => {
  it("shows the placement and the hostname in separate columns", () => {
    render(<NodeSelector nodes={nodes} selected={new Set()} onSelectionChange={vi.fn()} onOpenTerminal={vi.fn()} />);
    expect(screen.getByRole("columnheader", { name: "Hostname" })).toBeTruthy();
    const cell = screen.getByText("U20-ct01");
    expect(cell.getAttribute("title")).toBe("Placed at U20:11");
    const row = cell.closest("tr")!;
    expect(within(row).getByText("sys1-gso1-p-phy-gpu19-su19")).toBeTruthy();
    expect(within(row).getByText("UP")).toBeTruthy();
    expect(within(row).getByText("10.10.171.244")).toBeTruthy();
    expect(within(row).getByRole("button", { name: /terminal/i })).toBeTruthy();
  });

  it("falls back to the id for both columns on a legacy node", () => {
    render(<NodeSelector nodes={nodes} selected={new Set()} onSelectionChange={vi.fn()} />);
    const row = screen.getAllByText("p5-r01-ct01")[0].closest("tr")!;
    expect(within(row).getAllByText("p5-r01-ct01")).toHaveLength(2);
  });
});
