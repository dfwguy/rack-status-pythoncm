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
// detachState — carry a whole grid of shells into a new browser tab.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Encoded into the URL rather than handed over through storage. sessionStorage does not cross
// tabs at all, and localStorage would leave the detached tab reading state the original tab is
// still mutating -- two grids writing one key. A URL is self-contained: the new tab gets exactly
// the panes that existed at the moment the button was pressed, and it can be reloaded or
// bookmarked without depending on the tab that opened it.
//
// Sessions are carried by id so the detached tab ADOPTS the running shells instead of dialling
// fresh ones. Without that, detaching six panes would open six more SSH hops and leave the
// originals orphaned on the head node.
import type { LayoutItem } from "react-grid-layout";
import type { TerminalPaneSpec } from "./TerminalPane";

export interface DetachedGrid {
  panes: TerminalPaneSpec[];
  layout: LayoutItem[];
}

/** Only the geometry the grid needs, so the encoded URL stays short. */
interface CompactItem {
  i: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

interface Wire {
  v: 1;
  p: [string, string, string, string, string?][];
  l: CompactItem[];
}

/** base64url so the value survives a query string without percent-encoding noise. */
function toBase64Url(s: string): string {
  const b64 = btoa(unescape(encodeURIComponent(s)));
  return b64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64Url(s: string): string {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/");
  const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
  return decodeURIComponent(escape(atob(padded)));
}

export function encodeGrid(grid: DetachedGrid): string {
  const wire: Wire = {
    v: 1,
    p: grid.panes.map((p) => [p.id, p.target, p.mode, p.label, p.session]),
    l: grid.layout.map((l) => ({ i: l.i, x: l.x, y: l.y, w: l.w, h: l.h })),
  };
  return toBase64Url(JSON.stringify(wire));
}

/**
 * Decode a grid handed over in a URL.
 *
 * Returns null for anything malformed rather than throwing: this value arrives from a URL, which
 * a user can edit, truncate on copy/paste, or carry over from an older build. The detached tab
 * shows an empty grid in that case, which is recoverable; an exception during render is not.
 */
export function decodeGrid(encoded: string | null): DetachedGrid | null {
  if (!encoded) return null;
  try {
    const wire = JSON.parse(fromBase64Url(encoded)) as Wire;
    if (!wire || wire.v !== 1 || !Array.isArray(wire.p)) return null;

    const panes: TerminalPaneSpec[] = [];
    for (const entry of wire.p) {
      if (!Array.isArray(entry) || entry.length < 4) continue;
      const [id, target, mode, label, session] = entry;
      if (!id || !target) continue;
      panes.push({
        id: String(id),
        target: String(target),
        mode: mode === "headnode" ? "headnode" : "node",
        label: String(label || target),
        session: session ? String(session) : undefined,
      });
    }
    if (panes.length === 0) return null;

    const ids = new Set(panes.map((p) => p.id));
    const layout = (Array.isArray(wire.l) ? wire.l : [])
      .filter((l) => l && ids.has(String(l.i)))
      .map((l) => ({
        i: String(l.i),
        x: Number(l.x) || 0,
        y: Number(l.y) || 0,
        w: Number(l.w) || 6,
        h: Number(l.h) || 12,
      }));

    return { panes, layout };
  } catch {
    return null;
  }
}

/** URL for the detached grid tab. */
export function detachUrl(grid: DetachedGrid): string {
  return `/terminal-grid?g=${encodeGrid(grid)}`;
}
