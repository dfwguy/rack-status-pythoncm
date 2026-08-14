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
import { FooterNotes } from "./FooterNotes";

describe("FooterNotes", () => {
  it("renders footnotes", () => {
    render(<FooterNotes footnotes={["Note 1", "Note 2"]} />);
    expect(screen.getByText("Note 1")).toBeInTheDocument();
    expect(screen.getByText("Note 2")).toBeInTheDocument();
  });

  it("renders nothing when empty", () => {
    const { container } = render(<FooterNotes footnotes={[]} />);
    expect(container.firstChild).toBeNull();
  });
});
