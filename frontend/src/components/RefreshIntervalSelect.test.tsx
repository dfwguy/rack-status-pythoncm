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
import { RefreshIntervalSelect } from "./RefreshIntervalSelect";
import { DEFAULT_REFRESH_MS, REFRESH_OFF } from "../lib/refreshInterval";

describe("RefreshIntervalSelect", () => {
  it("shows the current interval as its label", () => {
    render(<RefreshIntervalSelect valueMs={DEFAULT_REFRESH_MS} onChangeMs={vi.fn()} aria-label="Auto-refresh interval" />);
    expect(screen.getAllByText("5m").length).toBeGreaterThan(0);
  });

  it("shows Off when auto-refresh is disabled", () => {
    render(<RefreshIntervalSelect valueMs={REFRESH_OFF} onChangeMs={vi.fn()} aria-label="Auto-refresh interval" />);
    expect(screen.getAllByText("Off").length).toBeGreaterThan(0);
  });

  it("offers every supported interval", () => {
    render(<RefreshIntervalSelect valueMs={DEFAULT_REFRESH_MS} onChangeMs={vi.fn()} aria-label="Auto-refresh interval" />);
    for (const label of ["Off", "1m", "5m", "15m", "30m"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
  });
});
