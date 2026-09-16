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
import { RackLocksContext } from "../context/RackLocksContext";
import { fetchSlurmJson } from "../lib/slurmHelpers";
import { EventsView } from "./EventsView";

vi.mock("../lib/slurmHelpers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/slurmHelpers")>();
  return { ...actual, fetchSlurmJson: vi.fn() };
});
vi.mock("../lib/useGrafanaDeeplinks", () => ({ useGrafanaDeeplinks: () => null }));
vi.mock("../components/layout/PageHeader", () => ({
  PageHeader: ({ title }: { title: string }) => <h1>{title}</h1>,
}));

describe("EventsView fleet pagination", () => {
  it("renders one page instead of thousands of current-state rows", async () => {
    vi.mocked(fetchSlurmJson).mockResolvedValue({
      res: new Response("{}", { status: 200 }),
      data: {
        ok: true,
        nodes: Array.from({ length: 60 }, (_, index) => ({
          node: `node-${String(index).padStart(2, "0")}`,
          state: "drain",
          reason: `maintenance-${index}`,
          actor: "operator",
          timestamp: `2026-09-06T07:${String(index).padStart(2, "0")}:00Z`,
        })),
      },
    });

    render(
      <RackLocksContext.Provider value={{
        locks: {},
        me: "test",
        now: "",
        loading: false,
        error: null,
        isLockedByOther: () => null,
        getLock: () => null,
        refresh: vi.fn(async () => undefined),
      }}>
        <EventsView />
      </RackLocksContext.Provider>,
    );

    expect(await screen.findByText("node-59")).toBeInTheDocument();
    expect(screen.getAllByText(/60 events/).length).toBeGreaterThan(0);
    expect(screen.getByText(/1–25 of 60 events/)).toBeInTheDocument();
    expect(screen.queryByText("node-34")).not.toBeInTheDocument();
  });
});
