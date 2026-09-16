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
import { describe, expect, it } from "vitest";
import { RackFloorView } from "./RackFloorView";

describe("RackFloorView node grid", () => {
  it("renders an aggregate-only 18-node PythonCM rack as two rows of nine", () => {
    render(
      <RackFloorView
        rows={[
          {
            rack: "ink50-dh02-a01",
            nemo: "",
            last_job: "",
            sinfo: "idle=18",
            ct_down_unk: "",
          },
        ]}
      />,
    );

    const grid = screen.getByLabelText("ink50-dh02-a01 node status grid");
    expect(grid).toHaveStyle({
      display: "grid",
      gridTemplateColumns: "repeat(9, minmax(0, 1fr))",
    });
    expect(grid.children).toHaveLength(18);
  });
});
