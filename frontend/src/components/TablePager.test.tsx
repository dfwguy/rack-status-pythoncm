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
import { TablePager } from "./TablePager";

const base = {
  page: 1,
  setPage: vi.fn(),
  pageSize: 25,
  setPageSize: vi.fn(),
  total: 100,
  hasPages: true,
  noun: "racks",
};

describe("TablePager", () => {
  it("renders nothing when everything fits on one page", () => {
    /**
     * This is what lets the same pager sit under every table in the app: a rack's 18 trays or a
     * five-row bring-up summary must not grow a pager, while a 2000-row squeue does.
     */
    const { container } = render(<TablePager {...base} total={10} hasPages={false} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("reports the range in terms of what the rows are", () => {
    render(<TablePager {...base} />);
    expect(screen.getByText(/1–25 of 100 racks/)).toBeInTheDocument();
  });

  it("names itself for assistive tech using the same noun", () => {
    render(<TablePager {...base} noun="jobs" />);
    expect(screen.getByLabelText("jobs pagination")).toBeInTheDocument();
  });

  it("counts the filtered total, not the payload size", () => {
    // total is whatever the caller paged, which is the post-filter list.
    render(<TablePager {...base} total={7} pageSize={5} />);
    expect(screen.getByText(/1–5 of 7 racks/)).toBeInTheDocument();
  });
});
