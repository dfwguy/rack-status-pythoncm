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
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StatusSummary } from "./StatusSummary";

describe("StatusSummary", () => {
  const defaultProps = {
    summary: { done: 10, fail: 2, ongoing: 3, pending: 3 },
    updatedAt: new Date().toISOString(),
  };

  it("renders rack count", () => {
    render(<StatusSummary {...defaultProps} />);
    expect(screen.getByText("18 racks total")).toBeInTheDocument();
  });

  it("renders all status tags", () => {
    render(<StatusSummary {...defaultProps} />);
    expect(screen.getByText("10 done")).toBeInTheDocument();
    expect(screen.getByText("2 fail")).toBeInTheDocument();
    expect(screen.getByText("3 ongoing")).toBeInTheDocument();
    expect(screen.getByText("3 pending")).toBeInTheDocument();
  });

  it("renders legend when provided", () => {
    render(<StatusSummary {...defaultProps} legend="Test legend text" />);
    expect(screen.getByText("Test legend text")).toBeInTheDocument();
  });

  it("does not render legend when absent", () => {
    render(<StatusSummary {...defaultProps} />);
    expect(screen.queryByText("Test legend text")).not.toBeInTheDocument();
  });
});
