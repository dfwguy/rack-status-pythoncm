/*
 * SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { PostRunNode } from "../../types/api";
import type { NodeValidationState } from "../../lib/validationStatus";
import { ValidationFloorMap } from "./ValidationFloorMap";

const node = {
  node: "ink50-dh02-a01-11",
  rack: "ink50-dh02-a01",
} as PostRunNode;

const state: NodeValidationState = {
  status: "pass",
  running: false,
  points: [],
  visiblePoints: [],
  tooltip: "ink50-dh02-a01-11 — PASS",
};

describe("ValidationFloorMap", () => {
  it("uses one responsive control per rack and keeps node names out of the visualization", () => {
    const onNode = vi.fn();
    const nodes = Array.from({ length: 18 }, (_, index) => ({
      node: { ...node, node: `ink50-dh02-a01-${String(index + 11).padStart(2, "0")}` },
      state: { ...state, tooltip: `ink50-dh02-a01-${String(index + 11).padStart(2, "0")} — PASS` },
    }));
    render(
      <ValidationFloorMap
        groups={[{ id: "SU1", rowKey: "dh02-a", racks: [{ rack: node.rack, nodes }] }]}
        onNode={onNode}
      />,
    );

    expect(screen.getByText("01")).toBeInTheDocument();
    expect(screen.queryByText("a01-11")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/zoom/i)).not.toBeInTheDocument();
    const controls = screen.getAllByRole("button");
    expect(controls).toHaveLength(1);
    expect(controls[0]).toHaveAccessibleName(/Rack ink50-dh02-a01: pass, 18\/18 passed/i);
    fireEvent.click(controls[0]);
    expect(onNode).toHaveBeenCalledWith(nodes[0].node);
  });
});
