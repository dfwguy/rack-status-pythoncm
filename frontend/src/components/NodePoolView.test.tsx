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
/**
 * The flat-pool render for a cluster with no placement data. It must: say so honestly (not draw a
 * fake rack), order nodes by hostname numerically (so gaps in gpu-rno-NNN read naturally), show the
 * per-node GPU count when known, and surface an inventory error without hiding the roster.
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { NodePoolView } from "./NodePoolView";
import type { NodeInventoryEntry } from "../types/api";

const node = (over: Partial<NodeInventoryEntry>): NodeInventoryEntry => ({
  id: "gpu-rno-248",
  rack: null,
  status: "up",
  ip: "",
  ...over,
});

describe("NodePoolView", () => {
  it("states there is no placement data rather than inventing a rack", () => {
    render(<NodePoolView nodes={[node({})]} />);
    expect(screen.getByText(/no rack placement data/i)).toBeInTheDocument();
    expect(screen.getByText(/flat node pool/i)).toBeInTheDocument();
  });

  it("orders nodes by hostname numerically and shows GPU counts", () => {
    render(
      <NodePoolView
        nodes={[
          node({ id: "gpu-rno-301", gpu_count: 8, gpu_type: "nvidia" }),
          node({ id: "gpu-rno-248", gpu_count: 8 }),
        ]}
      />
    );
    const mono = screen.getAllByText(/^gpu-rno-/);
    // numeric sort: 248 before 301, not lexicographic.
    expect(mono[0]).toHaveTextContent("gpu-rno-248");
    expect(mono[1]).toHaveTextContent("gpu-rno-301");
    expect(screen.getAllByText("8 GPU")).toHaveLength(2);
  });

  it("summarizes node, up and GPU totals", () => {
    render(
      <NodePoolView
        nodes={[
          node({ id: "a", status: "up", gpu_count: 8 }),
          node({ id: "b", status: "down", gpu_count: 8 }),
        ]}
      />
    );
    expect(screen.getByText(/2 nodes · 1 up · 16 GPUs/)).toBeInTheDocument();
  });

  it("falls back to a dash when a node's GPU count is unknown", () => {
    render(<NodePoolView nodes={[node({ id: "a" })]} />);
    // No gpu_count -> em dash, and no total GPUs clause in the summary.
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.getByText(/1 nodes · 1 up$/)).toBeInTheDocument();
  });

  it("reports unavailable inventory distinctly from an empty pool", () => {
    const { rerender } = render(<NodePoolView nodes={null} />);
    expect(screen.getByText(/inventory is unavailable/i)).toBeInTheDocument();

    rerender(<NodePoolView nodes={[]} />);
    expect(screen.getByText(/no nodes reported yet/i)).toBeInTheDocument();
  });

  it("surfaces an inventory error while still showing the roster", () => {
    render(<NodePoolView nodes={[node({ id: "a" })]} inventoryError="Showing the last successful inventory." />);
    expect(screen.getByText(/last successful inventory/i)).toBeInTheDocument();
    expect(within(screen.getByText("a")).queryByText("a")).toBeTruthy();
  });
});
