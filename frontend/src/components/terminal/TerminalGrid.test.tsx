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
// Renders the real grid against react-grid-layout. That matters more than it sounds: v2 replaced
// v1's flat props (cols, rowHeight, draggableHandle, isDraggable) with grouped config objects, so
// code written against the older API type-checks in places and still silently loses dragging or
// lays out at the wrong size. Only mounting it proves the props are wired to the version installed.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { TerminalGrid } from "./TerminalGrid";
import { PANE_DRAG_HANDLE } from "./TerminalPane";
import { autoLayout } from "./gridLayout";
import { addPane, EMPTY_GRID, withSession } from "./gridState";
import type { TerminalPaneSpec } from "./TerminalPane";

// xterm needs a real canvas and a WebSocket, neither of which jsdom provides. The hook itself is
// exercised by the existing standalone terminal; here it only has to report a stable shape.
const { useTerminalMock } = vi.hoisted(() => ({ useTerminalMock: vi.fn() }));
// Spread the real module so its other exports (TERMINAL_BACKGROUND) survive the mock; listing
// only useTerminal makes every consumer of a sibling export fail to import.
vi.mock("../../hooks/useTerminal", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../hooks/useTerminal")>()),
  useTerminal: useTerminalMock,
}));

beforeEach(() => {
  useTerminalMock.mockReset();
  useTerminalMock.mockReturnValue({ sessionId: null, connected: true, cleanup: vi.fn() });
});

const pane = (id: string, label = id): TerminalPaneSpec => ({
  id,
  target: label,
  mode: "node",
  label,
});

function renderGrid(panes: TerminalPaneSpec[], onChange = vi.fn()) {
  const layout = autoLayout(panes.map((p) => p.id));
  const utils = render(
    <TerminalGrid
      panes={panes}
      layout={layout}
      width={1200}
      onChange={onChange}
      onSession={vi.fn()}
    />,
  );
  return { ...utils, onChange };
}

describe("rendering panes", () => {
  it("shows a shell for every pane", () => {
    renderGrid([pane("a", "head-01"), pane("b", "ct07-01")]);
    expect(screen.getByText("head-01")).toBeTruthy();
    expect(screen.getByText("ct07-01")).toBeTruthy();
  });

  it("opens one terminal connection per pane, not one for the grid", () => {
    renderGrid([pane("a"), pane("b"), pane("c")]);
    expect(useTerminalMock).toHaveBeenCalledTimes(3);
  });

  it("invites the operator to add one when empty instead of rendering a blank area", () => {
    render(
      <TerminalGrid panes={[]} layout={[]} width={1200} onChange={vi.fn()} onSession={vi.fn()} />,
    );
    expect(screen.getByText(/No terminals open/i)).toBeTruthy();
  });

  it("reports each pane's connection state", () => {
    useTerminalMock.mockReturnValue({ sessionId: null, connected: false, cleanup: vi.fn() });
    renderGrid([pane("a")]);
    expect(screen.getByText("offline")).toBeTruthy();
  });
});

describe("drag surface", () => {
  it("marks only the header as the drag handle, so terminal text stays selectable", () => {
    const { container } = renderGrid([pane("a")]);
    const handles = container.querySelectorAll(`.${PANE_DRAG_HANDLE}`);
    expect(handles).toHaveLength(1);
    // The handle must not be the pane itself, or the whole terminal becomes a drag surface.
    const body = container.querySelector(".react-grid-item");
    expect(body?.classList.contains(PANE_DRAG_HANDLE)).toBe(false);
  });

  it("hands each pane to the grid as a positioned item", () => {
    const { container } = renderGrid([pane("a"), pane("b")]);
    // Proves the layout prop was accepted by the installed version and items were placed.
    expect(container.querySelectorAll(".react-grid-item")).toHaveLength(2);
  });
});

describe("closing a pane", () => {
  it("removes just that pane and keeps the rest", () => {
    const onChange = vi.fn();
    renderGrid([pane("a", "head-01"), pane("b", "ct07-01")], onChange);

    fireEvent.click(screen.getByLabelText("Close ct07-01"));

    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0];
    expect(next.panes.map((p: TerminalPaneSpec) => p.label)).toEqual(["head-01"]);
  });

  it("gives the survivor the full width so no half-empty row is left behind", () => {
    const onChange = vi.fn();
    renderGrid([pane("a"), pane("b")], onChange);
    fireEvent.click(screen.getByLabelText("Close b"));
    expect(onChange.mock.calls[0][0].layout[0].w).toBe(12);
  });
});

describe("detaching a single pane", () => {
  // The detached tab is opened blank inside the click and navigated once the session is known to
  // be fresh (openApiUrl), so the URL is read from where the tab ends up, not from window.open.
  function openDetachedTab() {
    const tab = { opener: {} as unknown, location: { href: "about:blank" }, close: vi.fn() };
    const open = vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ authenticated: true }), { status: 200 })));
    return {
      open,
      tab,
      restore: () => {
        open.mockRestore();
        vi.unstubAllGlobals();
      },
    };
  }

  it("carries the live session so the new tab adopts the shell", async () => {
    useTerminalMock.mockReturnValue({ sessionId: "sess-9", connected: true, cleanup: vi.fn() });
    const { open, tab, restore } = openDetachedTab();
    renderGrid([pane("a", "head-01")]);

    fireEvent.click(screen.getByLabelText("Detach head-01"));

    expect(open).toHaveBeenCalledWith("about:blank", "_blank");
    await waitFor(() => expect(tab.location.href).toContain("/terminal?"));
    expect(tab.location.href).toContain("session=sess-9");
    // A target alongside the session would let the new tab dial a second shell.
    expect(tab.location.href).not.toContain("target=");
    restore();
  });

  it("falls back to the target when no session has been assigned yet", async () => {
    const { tab, restore } = openDetachedTab();
    renderGrid([pane("a", "ct07-01")]);
    fireEvent.click(screen.getByLabelText("Detach ct07-01"));
    await waitFor(() => expect(tab.location.href).toContain("target=ct07-01"));
    expect(tab.location.href).toContain("mode=node");
    restore();
  });
});

describe("a pane must not reconnect once its session is recorded", () => {
  // useTerminal reconnects when its target or sessionId changes. The parent records the session a
  // pane reports and passes it back down, so a pane that derived its connection inputs from the
  // current spec would drop its WebSocket the moment it finished connecting. Invisible in types
  // and in a static render; this is the only place it is caught.
  it("keeps calling useTerminal with the inputs it was created with", () => {
    useTerminalMock.mockReturnValue({ sessionId: "sess-1", connected: true, cleanup: vi.fn() });
    const spec = pane("a", "ct07-01");
    const layout = autoLayout(["a"]);

    const { rerender } = render(
      <TerminalGrid
        panes={[spec]}
        layout={layout}
        width={1200}
        onChange={vi.fn()}
        onSession={vi.fn()}
      />,
    );

    // Exactly what the parent does after the pane reports its session.
    rerender(
      <TerminalGrid
        panes={[{ ...spec, session: "sess-1" }]}
        layout={layout}
        width={1200}
        onChange={vi.fn()}
        onSession={vi.fn()}
      />,
    );

    const calls = useTerminalMock.mock.calls.map(([args]) => ({
      target: args.target,
      sessionId: args.sessionId,
    }));
    // Every call carries the original target and no session id; a changed pair here is a reconnect.
    for (const call of calls) {
      expect(call.target).toBe("ct07-01");
      expect(call.sessionId).toBeUndefined();
    }
  });

  it("adopts a session it was created with, rather than dialling the target", () => {
    renderGrid([{ ...pane("a", "ct07-01"), session: "existing" }]);
    const [args] = useTerminalMock.mock.calls[0];
    expect(args.sessionId).toBe("existing");
    // A target alongside the session would open a second shell on the node.
    expect(args.target).toBeUndefined();
  });
});

describe("grid state operations", () => {
  it("gives two shells on the same node separate panes", () => {
    const one = addPane(EMPTY_GRID, { target: "ct07-01", mode: "node", label: "ct07-01" });
    const two = addPane(one, { target: "ct07-01", mode: "node", label: "ct07-01" });
    expect(two.panes).toHaveLength(2);
    expect(two.panes[0].id).not.toBe(two.panes[1].id);
  });

  it("returns the identical object when a session is re-reported, so renders cannot loop", () => {
    const added = addPane(EMPTY_GRID, { target: "n1", mode: "node", label: "n1" });
    const id = added.panes[0].id;
    const first = withSession(added, id, "s1");
    expect(first).not.toBe(added);
    expect(withSession(first, id, "s1")).toBe(first);
  });

  it("ignores a session for a pane that has already been closed", () => {
    const state = addPane(EMPTY_GRID, { target: "n1", mode: "node", label: "n1" });
    expect(withSession(state, "gone", "s1")).toBe(state);
  });
});
