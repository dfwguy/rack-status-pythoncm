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
import { describe, expect, it, vi } from "vitest";
import { NodeSelector } from "./NodeSelector";

describe("NodeSelector Slurm status", () => {
  it("replaces the empty Actions column with live Slurm state and reason", () => {
    const refresh = vi.fn();
    render(
      <NodeSelector
        nodes={[{ id: "ink50-dh02-h02-15", status: "down" }]}
        selected={new Set(["ink50-dh02-h02-15"])}
        onSelectionChange={vi.fn()}
        slurmByNode={{
          "ink50-dh02-h02-15": {
            node: "ink50-dh02-h02-15",
            state: "drained",
            reason: "Prolog error",
          },
        }}
        slurmUpdatedAt="12:34:56 PM"
        onRefreshSlurm={refresh}
      />,
    );

    expect(screen.getByText("Slurm Status")).toBeInTheDocument();
    expect(screen.queryByText("Actions")).not.toBeInTheDocument();
    expect(screen.getByText("DRAINED")).toBeInTheDocument();
    expect(screen.getByText("Prolog error")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh live Slurm status" }));
    expect(refresh).toHaveBeenCalledOnce();
  });
});
