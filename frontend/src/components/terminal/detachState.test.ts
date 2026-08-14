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
// The detach payload arrives from a URL, which means it is untrusted input: a user can edit it, a
// copy/paste can truncate it, and an older build's link can outlive its format. Every one of those
// must produce an empty grid, never an exception during render.
import { describe, it, expect } from "vitest";
import { decodeGrid, encodeGrid, detachUrl } from "./detachState";
import type { DetachedGrid } from "./detachState";

const grid: DetachedGrid = {
  panes: [
    { id: "a", target: "headnode", mode: "headnode", label: "Head Node", session: "sess-1" },
    { id: "b", target: "ink50-dh01-h01-11", mode: "node", label: "ink50-dh01-h01-11" },
  ],
  layout: [
    { i: "a", x: 0, y: 0, w: 6, h: 12 },
    { i: "b", x: 6, y: 0, w: 6, h: 12 },
  ],
};

describe("encode / decode round trip", () => {
  it("preserves every pane and its geometry", () => {
    const out = decodeGrid(encodeGrid(grid));
    expect(out).not.toBeNull();
    expect(out!.panes).toEqual(grid.panes);
    expect(out!.layout).toEqual(grid.layout);
  });

  it("carries the live session id so the new tab adopts the shell", () => {
    const out = decodeGrid(encodeGrid(grid));
    expect(out!.panes[0].session).toBe("sess-1");
  });

  it("leaves a pane with no session undefined rather than inventing one", () => {
    const out = decodeGrid(encodeGrid(grid));
    expect(out!.panes[1].session).toBeUndefined();
  });

  it("survives a hostname with characters a URL would otherwise mangle", () => {
    const odd: DetachedGrid = {
      panes: [{ id: "x", target: "node+01/a", mode: "node", label: "node+01/a — ünïcode" }],
      layout: [{ i: "x", x: 0, y: 0, w: 12, h: 12 }],
    };
    expect(decodeGrid(encodeGrid(odd))!.panes[0]).toEqual(odd.panes[0]);
  });

  it("produces a URL the detached route can read", () => {
    const url = detachUrl(grid);
    expect(url.startsWith("/terminal-grid?g=")).toBe(true);
    // base64url only — a raw + or / here would be corrupted in transit.
    const payload = url.split("=")[1];
    expect(payload).toMatch(/^[A-Za-z0-9_-]+$/);
  });
});

describe("malformed input is refused, not thrown on", () => {
  it.each([
    ["null", null],
    ["empty", ""],
    ["not base64", "!!!!"],
    ["base64 of nonsense", btoa("hello there")],
    ["truncated payload", encodeGrid(grid).slice(0, 12)],
    ["valid JSON, wrong shape", btoa(JSON.stringify({ nope: true }))],
    ["a future version", btoa(JSON.stringify({ v: 99, p: [], l: [] }))],
    ["an empty pane list", btoa(JSON.stringify({ v: 1, p: [], l: [] }))],
  ])("returns null for %s", (_label, value) => {
    expect(decodeGrid(value as string | null)).toBeNull();
  });

  it("drops pane entries missing an id or target instead of rendering a broken pane", () => {
    const wire = { v: 1, p: [["", "n1", "node", "n1"], ["id2", "", "node", ""]], l: [] };
    expect(decodeGrid(btoa(JSON.stringify(wire)))).toBeNull();
  });

  it("keeps the good panes when only some entries are broken", () => {
    const wire = {
      v: 1,
      p: [["ok", "n1", "node", "n1"], ["", "", "", ""]],
      l: [{ i: "ok", x: 0, y: 0, w: 12, h: 12 }],
    };
    const out = decodeGrid(btoa(JSON.stringify(wire)));
    expect(out!.panes.map((p) => p.id)).toEqual(["ok"]);
  });

  it("discards layout items that reference a pane that is not there", () => {
    const wire = {
      v: 1,
      p: [["ok", "n1", "node", "n1"]],
      l: [{ i: "ok", x: 0, y: 0, w: 6, h: 12 }, { i: "ghost", x: 6, y: 0, w: 6, h: 12 }],
    };
    const out = decodeGrid(btoa(JSON.stringify(wire)));
    expect(out!.layout.map((l) => l.i)).toEqual(["ok"]);
  });

  it("coerces an unknown mode to a compute node rather than trusting it", () => {
    const wire = { v: 1, p: [["a", "n1", "root-shell", "n1"]], l: [] };
    expect(decodeGrid(btoa(JSON.stringify(wire)))!.panes[0].mode).toBe("node");
  });
});
