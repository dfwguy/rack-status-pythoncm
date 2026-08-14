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
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// The detached tab is opened by window.open and renders outside the router and the app shell, so
// nothing in the dashboard reports when it fails -- the operator just gets a blank tab. These
// tests mount it the way the browser does, from a URL, which is the only cheap way to catch a
// throw on that path.
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import TerminalGridTabView from "./TerminalGridTabView";
import { encodeGrid } from "../components/terminal/detachState";

const { useTerminalMock } = vi.hoisted(() => ({ useTerminalMock: vi.fn() }));
vi.mock("../hooks/useTerminal", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../hooks/useTerminal")>()),
  useTerminal: useTerminalMock,
}));

const grid = {
  panes: [
    { id: "a", target: "headnode", mode: "headnode" as const, label: "Head Node", session: "s1" },
    { id: "b", target: "ink50-dh01-h01-14", mode: "node" as const, label: "ink50-dh01-h01-14" },
  ],
  layout: [
    { i: "a", x: 0, y: 0, w: 6, h: 12 },
    { i: "b", x: 6, y: 0, w: 6, h: 12 },
  ],
};

const originalLocation = window.location;

function setUrl(search: string) {
  Object.defineProperty(window, "location", {
    writable: true,
    value: { ...originalLocation, pathname: "/terminal-grid", search, href: `http://x/terminal-grid${search}` },
  });
}

beforeEach(() => {
  useTerminalMock.mockReset();
  useTerminalMock.mockReturnValue({ sessionId: "s1", connected: true, cleanup: vi.fn() });
});

afterEach(() => {
  Object.defineProperty(window, "location", { writable: true, value: originalLocation });
});

describe("the detached grid tab renders from its URL", () => {
  it("shows every pane handed over", () => {
    setUrl(`?g=${encodeGrid(grid)}`);
    render(<TerminalGridTabView />);
    expect(screen.getByText("Head Node")).toBeTruthy();
    expect(screen.getByText("ink50-dh01-h01-14")).toBeTruthy();
  });

  it("adopts the sessions rather than dialling the targets again", () => {
    setUrl(`?g=${encodeGrid(grid)}`);
    render(<TerminalGridTabView />);
    const args = useTerminalMock.mock.calls.map(([a]) => a);
    const head = args.find((a) => a.label === "Head Node");
    expect(head.sessionId).toBe("s1");
    expect(head.target).toBeUndefined();
  });

  it("explains itself instead of going blank when the payload is missing", () => {
    setUrl("");
    render(<TerminalGridTabView />);
    expect(screen.getByText(/carries no terminals/i)).toBeTruthy();
  });

  it("explains itself instead of throwing when the payload is corrupt", () => {
    setUrl("?g=not-a-valid-payload");
    render(<TerminalGridTabView />);
    expect(screen.getByText(/carries no terminals/i)).toBeTruthy();
  });

  it("reports how many shells it holds", () => {
    setUrl(`?g=${encodeGrid(grid)}`);
    render(<TerminalGridTabView />);
    expect(screen.getByText(/2 open/)).toBeTruthy();
  });
});
