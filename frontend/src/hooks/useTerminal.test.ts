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

/**
 * The terminal WebSocket is opened only after `ensureFreshSession()`. The upgrade request carries
 * the access cookie but cannot renew it, so a terminal opened an hour into a session failed to
 * connect while every other panel (renewed by `apiFetch`) kept working.
 */
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ensureFreshSession = vi.fn<() => Promise<boolean>>();
const writeln = vi.fn();

vi.mock("../lib/apiFetch", () => ({ ensureFreshSession: () => ensureFreshSession() }));
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));
vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    cols = 80;
    rows = 24;
    unicode = { activeVersion: "" };
    loadAddon() {}
    open() {}
    writeln(s: string) {
      writeln(s);
    }
    write() {}
    focus() {}
    dispose() {}
    onData() {}
    onResize() {}
  },
}));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: class { fit() {} } }));
vi.mock("@xterm/addon-webgl", () => ({ WebglAddon: class {} }));
vi.mock("@xterm/addon-unicode11", () => ({ Unicode11Addon: class {} }));
vi.mock("@xterm/addon-web-links", () => ({ WebLinksAddon: class {} }));

import { useTerminal } from "./useTerminal";

const sockets: string[] = [];

class FakeWebSocket {
  static OPEN = 1;
  readyState = 0;
  binaryType = "blob";
  onopen: (() => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(url: string) {
    sockets.push(url);
  }
  send() {}
  close() {}
}

beforeEach(() => {
  sockets.length = 0;
  writeln.mockReset();
  ensureFreshSession.mockReset();
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function mount() {
  const containerRef = { current: document.createElement("div") };
  return renderHook(() => useTerminal({ containerRef, target: "node01", mode: "node", active: true }));
}

describe("useTerminal", () => {
  it("does not open the WebSocket until the session has been renewed", async () => {
    let resolve: (fresh: boolean) => void = () => {};
    ensureFreshSession.mockReturnValue(new Promise<boolean>((r) => { resolve = r; }));

    mount();
    await waitFor(() => expect(ensureFreshSession).toHaveBeenCalledTimes(1));
    expect(sockets).toEqual([]);

    resolve(true);
    await waitFor(() => expect(sockets).toHaveLength(1));
    expect(sockets[0]).toMatch(/\/ws\/terminal\?target=node01&mode=node$/);
  });

  it("never opens the WebSocket when the session is gone, and says why", async () => {
    ensureFreshSession.mockResolvedValue(false);

    mount();

    await waitFor(() => expect(writeln).toHaveBeenCalledWith(expect.stringContaining("session has expired")));
    expect(sockets).toEqual([]);
  });

  it("does not open a socket for a pane closed while the check was pending", async () => {
    let resolve: (fresh: boolean) => void = () => {};
    ensureFreshSession.mockReturnValue(new Promise<boolean>((r) => { resolve = r; }));

    const { unmount } = mount();
    await waitFor(() => expect(ensureFreshSession).toHaveBeenCalledTimes(1));
    unmount();
    resolve(true);
    await new Promise((r) => setTimeout(r, 20));

    expect(sockets).toEqual([]);
  });
});
