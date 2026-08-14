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
// TerminalPane — one live shell inside the terminal grid.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Wraps the existing useTerminal hook rather than reimplementing xterm wiring, so a pane inherits
// the behaviour already proven in the standalone tab and the modal: WebSocket session, refit on
// container resize (which is what makes it work inside a resizable grid cell), and reconnect by
// session id.
//
// The header is the drag handle, deliberately. If the whole pane were draggable you could not
// select text in the terminal — a drag would start instead of a selection, which breaks the most
// common thing an operator does with output.
import { useEffect, useRef, useState } from "react";
import { Tag, Text } from "@nvidia/foundations-react-core";
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import Box from "@mui/material/Box";
import { ExternalLink, GripVertical, X } from "lucide-react";
import { TERMINAL_BACKGROUND, useTerminal } from "../../hooks/useTerminal";

/** Class the grid uses as its drag handle; only elements carrying it start a drag. */
export const PANE_DRAG_HANDLE = "nvis-pane-drag";

export interface TerminalPaneSpec {
  /** Stable identity for React keys and grid layout items. Must not change when panes move. */
  id: string;
  /** Node hostname, or the head node sentinel. */
  target: string;
  mode: "node" | "headnode";
  label: string;
  /** Backend session id once connected — carried when detaching so the shell is reused. */
  session?: string;
}

interface TerminalPaneProps {
  spec: TerminalPaneSpec;
  onClose: (id: string) => void;
  /** Called with the backend session id so the parent can carry it when detaching. */
  onSession?: (id: string, sessionId: string) => void;
}

export function TerminalPane({ spec, onClose, onSession }: TerminalPaneProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  // Frozen on the first render, and deliberately not derived from spec on later ones.
  //
  // useTerminal reconnects whenever its target or sessionId changes. The parent records the
  // session id this pane reports, which flows back down as spec.session -- so deriving these
  // inline made every pane tear down its WebSocket and reconnect the instant it finished
  // connecting, clearing the screen and printing "Reconnecting" over its own first output.
  // A pane connects with the inputs it was created with; the parent's bookkeeping is for detach.
  const [connect] = useState(() => ({
    target: spec.session ? undefined : spec.target,
    session: spec.session,
  }));

  const { sessionId, connected } = useTerminal({
    containerRef,
    target: connect.target,
    mode: spec.mode,
    label: spec.label,
    sessionId: connect.session,
    active: true,
  });

  // Hand the session id to the parent, which needs it to detach this shell rather than dial a
  // second one. Reported from an effect, not during render: a render-phase notify would both
  // touch a ref while rendering and call into the parent's setState mid-render, which React 19
  // rejects outright. The parent ignores a repeat of the same id, so this settles after one pass.
  useEffect(() => {
    if (sessionId) onSession?.(spec.id, sessionId);
  }, [sessionId, spec.id, onSession]);

  const detach = () => {
    const params = new URLSearchParams();
    // Prefer the live session so the detached tab adopts this shell rather than opening a second
    // one on the same node, which would leave two SSH hops where the operator expects a move.
    if (sessionId) params.set("session", sessionId);
    else {
      params.set("target", spec.target);
      params.set("mode", spec.mode);
    }
    params.set("label", spec.label);
    window.open(`/terminal?${params}`, "_blank", "noopener,noreferrer");
  };

  return (
    <Box
      sx={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        border: "1px solid #30363d",
        borderRadius: 1,
        background: TERMINAL_BACKGROUND,
      }}
    >
      <Box
        className={PANE_DRAG_HANDLE}
        sx={{
          display: "flex",
          alignItems: "center",
          gap: 1,
          px: 1,
          height: 32,
          flexShrink: 0,
          background: "#161b22",
          borderBottom: "1px solid #30363d",
          cursor: "move",
        }}
      >
        <GripVertical size={14} color="#7d8590" />
        <Text kind="mono/sm" style={{ color: "#e6edf3", whiteSpace: "nowrap" }}>
          {spec.label}
        </Text>
        <Tag color={connected ? "green" : "red"} kind="solid" readOnly density="compact">
          {connected ? "connected" : "offline"}
        </Tag>
        <Box sx={{ flex: 1 }} />
        <Tooltip title="Detach this terminal to a new tab">
          {/* onMouseDown stops the grid from reading the click as the start of a drag. */}
          <IconButton
            size="small"
            onMouseDown={(e) => e.stopPropagation()}
            onClick={detach}
            aria-label={`Detach ${spec.label}`}
            sx={{ color: "#e6edf3", p: 0.25 }}
          >
            <ExternalLink size={14} />
          </IconButton>
        </Tooltip>
        <Tooltip title="Close this terminal">
          <IconButton
            size="small"
            onMouseDown={(e) => e.stopPropagation()}
            onClick={() => onClose(spec.id)}
            aria-label={`Close ${spec.label}`}
            sx={{ color: "#e6edf3", p: 0.25 }}
          >
            <X size={14} />
          </IconButton>
        </Tooltip>
      </Box>

      {/* Two boxes on purpose: the inset lives on the outer one, and the terminal mounts in an
          inner box that carries NO padding.
          
          FitAddon sizes the shell from `term.element.parentElement` — the element xterm was opened
          on. Padding that element does not shrink what FitAddon measures, so it computed rows for
          the full height and the content was then displaced downwards by padding-top and clipped at
          the bottom: every pixel of padding added made the last line, the prompt, worse rather than
          better. Insetting from the outside instead reduces the inner box's height before it is
          measured, which is what actually gives the shell room to breathe. */}
      <Box
        sx={{
          flex: 1,
          minHeight: 0,
          minWidth: 0,
          overflow: "hidden",
          padding: "10px 12px 14px 12px",
          boxSizing: "border-box",
          display: "flex",
        }}
      >
        <Box ref={containerRef} sx={{ flex: 1, minHeight: 0, minWidth: 0, overflow: "hidden" }} />
      </Box>
    </Box>
  );
}
