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
import { useEffect, useRef, useCallback, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { WebglAddon } from "@xterm/addon-webgl";
import { Unicode11Addon } from "@xterm/addon-unicode11";
import { WebLinksAddon } from "@xterm/addon-web-links";
import "@xterm/xterm/css/xterm.css";

/**
 * Terminal background, shared by the xterm theme and every surface that hosts one.
 *
 * Exported so a pane, the standalone tab and the dialog cannot drift from the shell they contain —
 * a mismatch of even one shade shows up as a visible band around the content.
 */
export const TERMINAL_BACKGROUND = "#0d1117";

/** How long a connected-but-silent shell waits before it explains itself. */
const SILENCE_HINT_MS = 12_000;

export interface UseTerminalOptions {
  /** Ref to the container element where xterm.js will attach. */
  containerRef: React.RefObject<HTMLDivElement | null>;
  /** Target identifier sent to the backend (node name or "headnode"). */
  target?: string;
  /** "node" = SSH hop via head node, "headnode" = direct SSH to head node. */
  mode?: "node" | "headnode";
  /** Display label for connection messages. */
  label?: string;
  /** Existing session ID to reconnect to (skips creating a new session). */
  sessionId?: string;
  /** Whether the terminal should be active (mounted and connected). */
  active: boolean;
}

export interface UseTerminalResult {
  /** The session ID assigned by the backend (available after connection). */
  sessionId: string | null;
  /** Whether the WebSocket is currently connected. */
  connected: boolean;
  /** Manually disconnect and dispose the terminal. */
  cleanup: () => void;
}

export function useTerminal({
  containerRef,
  target,
  mode = "node",
  label,
  sessionId: reconnectSessionId,
  active,
}: UseTerminalOptions): UseTerminalResult {
  const termRef = useRef<Terminal | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(reconnectSessionId ?? null);
  const [connected, setConnected] = useState(false);

  const roRef = useRef<ResizeObserver | null>(null);
  const silenceTimersRef = useRef<ReturnType<typeof setTimeout>[]>([]);
  const resizeHandlerRef = useRef<(() => void) | null>(null);
  const focusHandlerRef = useRef<(() => void) | null>(null);

  const cleanup = useCallback(() => {
    for (const t of silenceTimersRef.current) clearTimeout(t);
    silenceTimersRef.current = [];
    if (roRef.current) {
      roRef.current.disconnect();
      roRef.current = null;
    }
    if (resizeHandlerRef.current) {
      window.removeEventListener("resize", resizeHandlerRef.current);
      resizeHandlerRef.current = null;
    }
    if (focusHandlerRef.current) {
      window.removeEventListener("focus", focusHandlerRef.current);
      focusHandlerRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    if (termRef.current) {
      termRef.current.dispose();
      termRef.current = null;
    }
    setConnected(false);
  }, []);

  useEffect(() => {
    if (!active) return;
    if (!reconnectSessionId && !target) return;

    const timerId = setTimeout(() => {
      const el = containerRef.current;
      if (!el) return;

      const term = new Terminal({
        allowProposedApi: true,
        cursorBlink: true,
        fontSize: 14,
        fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Menlo', monospace",
        theme: {
          background: TERMINAL_BACKGROUND,
          foreground: "#e6edf3",
          cursor: "#76b900",
          selectionBackground: "#264f78",
        },
        scrollback: 5000,
      });
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.loadAddon(new WebLinksAddon());
      const unicode11 = new Unicode11Addon();
      term.loadAddon(unicode11);
      term.unicode.activeVersion = "11";
      term.open(el);

      // Ensure xterm internals stretch to fill the container
      const xtermEl = el.querySelector(".xterm") as HTMLElement | null;
      if (xtermEl) { xtermEl.style.width = "100%"; xtermEl.style.height = "100%"; }
      const screenEl = el.querySelector(".xterm-screen") as HTMLElement | null;
      if (screenEl) { screenEl.style.width = "100%"; }
      // Force scrollbar always visible so FitAddon accounts for its width.
      // The background is set explicitly because xterm.css hardcodes `.xterm-viewport` to #000,
      // which the theme's background does not override — so any part of the viewport the rendered
      // rows do not cover (the leftover below the last line, and the scrollbar track) shows as a
      // pure-black band against the panel.
      const viewport = el.querySelector(".xterm-viewport") as HTMLElement | null;
      if (viewport) {
        viewport.style.overflowY = "scroll";
        viewport.style.width = "100%";
        viewport.style.backgroundColor = TERMINAL_BACKGROUND;
      }
      try {
        term.loadAddon(new WebglAddon());
      } catch {
        // WebGL not available
      }
      termRef.current = term;

      requestAnimationFrame(() => fit.fit());

      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const params = new URLSearchParams();
      if (reconnectSessionId) {
        params.set("session", reconnectSessionId);
      } else if (target) {
        params.set("target", target);
        params.set("mode", mode);
      }
      const wsUrl = `${protocol}//${window.location.host}/ws/terminal?${params}`;
      const ws = new WebSocket(wsUrl);
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;

      const displayName = label || target || "terminal";
      let receivedSession = false;

      ws.onopen = () => {
        setConnected(true);
        if (reconnectSessionId) {
          term.writeln(`\x1b[1;33mReconnecting to ${displayName}...\x1b[0m\r\n`);
        } else {
          term.writeln(`\x1b[1;32mConnected to ${displayName}\x1b[0m\r\n`);
        }
        ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
      };

      // A shell that connects and then says nothing is indistinguishable from a broken UI. It is a
      // real state on a cluster — an unreachable tray, or a login stalled on an NFS home — and
      // silence is the worst possible way to present it. Name the usual causes rather than leaving
      // an empty pane. Cleared the moment any byte arrives, so a healthy shell never shows it.
      let sawOutput = false;
      const silenceTimer = setTimeout(() => {
        if (!sawOutput) {
          term.writeln(
            `\x1b[1;33mNo output yet from ${displayName}.\x1b[0m Still waiting on the remote — a` +
            ` node that is down, or a login stalled on a shared filesystem, both look like this.`,
          );
        }
      }, SILENCE_HINT_MS);
      silenceTimersRef.current.push(silenceTimer);

      /** Terminal bytes only. The session/error control frames arrive at once and are not output. */
      const noteOutput = () => {
        if (sawOutput) return;
        sawOutput = true;
        clearTimeout(silenceTimer);
      };

      ws.onmessage = (evt) => {
        if (typeof evt.data === "string" && !receivedSession) {
          try {
            const msg = JSON.parse(evt.data);
            if (msg.type === "session" && msg.id) {
              setSessionId(msg.id);
              receivedSession = true;
              return;
            }
            if (msg.type === "error") {
              term.writeln(`\r\n\x1b[1;31mError: ${msg.error}\x1b[0m`);
              return;
            }
          } catch {
            // not JSON, treat as terminal data
          }
        }
        noteOutput();
        if (evt.data instanceof ArrayBuffer) {
          term.write(new Uint8Array(evt.data));
        } else {
          term.write(evt.data);
        }
      };

      ws.onclose = () => {
        setConnected(false);
        clearTimeout(silenceTimer);
        term.writeln("\r\n\x1b[1;31mConnection closed.\x1b[0m");
      };

      ws.onerror = () => {
        term.writeln("\r\n\x1b[1;31mWebSocket error.\x1b[0m");
      };

      term.onData((data) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(data);
      });

      term.onResize(({ cols, rows }) => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "resize", cols, rows }));
        }
      });

      let fitTimer: ReturnType<typeof setTimeout> | null = null;
      const doFit = () => {
        if (fitTimer) clearTimeout(fitTimer);
        fitTimer = setTimeout(() => {
          requestAnimationFrame(() => {
            try { fit.fit(); } catch { /* container might be detached */ }
          });
        }, 60);
      };

      const ro = new ResizeObserver(doFit);
      ro.observe(el);
      roRef.current = ro;

      resizeHandlerRef.current = doFit;
      window.addEventListener("resize", doFit);

      // Re-fit on focus — catches tab detach/attach where dimensions settle late
      const onFocus = () => {
        doFit();
        setTimeout(doFit, 250);
      };
      focusHandlerRef.current = onFocus;
      window.addEventListener("focus", onFocus);

      // Focus the terminal so cursor blinks and input is ready
      term.focus();

      // Staggered initial fits to catch late layout settling
      doFit();
      setTimeout(doFit, 300);
      setTimeout(() => { doFit(); term.focus(); }, 800);
    }, 150);

    return () => {
      clearTimeout(timerId);
      cleanup();
    };
  }, [active, target, mode, label, reconnectSessionId, containerRef, cleanup]);

  return { sessionId, connected, cleanup };
}
