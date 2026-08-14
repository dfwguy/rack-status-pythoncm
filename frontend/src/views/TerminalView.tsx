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
import { useRef, useEffect, useMemo } from "react";
import { Tag, Text } from "@nvidia/foundations-react-core";
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import { Terminal as TerminalIcon, X } from "lucide-react";
import { TERMINAL_BACKGROUND, useTerminal } from "../hooks/useTerminal";

const MIN_WIDTH = 640;
const MIN_HEIGHT = 400;

export default function TerminalView() {
  const searchParams = useMemo(() => new URLSearchParams(window.location.search), []);
  const containerRef = useRef<HTMLDivElement>(null);

  const session = searchParams.get("session") || undefined;
  const target = searchParams.get("target") || undefined;
  const mode = (searchParams.get("mode") as "node" | "headnode") || "node";
  const label = searchParams.get("label") || target || "Terminal";

  useEffect(() => {
    document.title = `Terminal — ${label}`;
    const s = document.documentElement.style;
    s.margin = "0";
    s.padding = "0";
    s.height = "100%";
    s.overflow = "hidden";
    const b = document.body.style;
    b.margin = "0";
    b.padding = "0";
    b.height = "100%";
    b.overflow = "hidden";
    b.background = TERMINAL_BACKGROUND;
    const root = document.getElementById("root");
    if (root) {
      root.style.height = "100%";
      root.style.overflow = "hidden";
    }
  }, [label]);

  const { connected } = useTerminal({
    containerRef,
    target,
    mode,
    label,
    sessionId: session,
    active: true,
  });

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        width: "100vw",
        height: "100vh",
        minWidth: MIN_WIDTH,
        minHeight: MIN_HEIGHT,
        background: TERMINAL_BACKGROUND,
        overflow: "hidden",
        position: "absolute",
        inset: 0,
      }}
    >
      {/* Toolbar */}
      <div
        style={{
          display: "flex",
          flexDirection: "row",
          alignItems: "center",
          gap: 8,
          padding: "4px 12px",
          background: "#161b22",
          borderBottom: "1px solid #30363d",
          flexShrink: 0,
          height: 36,
          boxSizing: "border-box",
        }}
      >
        <TerminalIcon size={16} color="#76b900" />
        <Text kind="body/bold/sm" style={{ color: "#e6edf3" }}>Terminal</Text>
        <Tag color="green" kind="outline" readOnly density="compact">{label}</Tag>

        {connected ? (
          <Tag color="green" kind="solid" readOnly density="compact">connected</Tag>
        ) : (
          <Tag color="red" kind="solid" readOnly density="compact">disconnected</Tag>
        )}

        <div style={{ flex: 1 }} />

        <Tooltip title="Close terminal">
          <IconButton size="small" onClick={() => window.close()} sx={{ color: "#e6edf3" }}>
            <X size={16} />
          </IconButton>
        </Tooltip>
      </div>

      {/* Terminal container. The inset is on this outer box and the terminal mounts in an unpadded
          child: FitAddon sizes the shell from the element xterm was opened on, so padding *that*
          element leaves the row count computed for the full height and displaces the content down
          by padding-top, clipping the last line. Inset from outside instead. */}
      <div
        style={{
          flex: 1,
          width: "100%",
          minHeight: 0,
          minWidth: 0,
          overflow: "hidden",
          padding: "4px 4px 8px 8px",
          boxSizing: "border-box",
          display: "flex",
        }}
      >
        <div
          ref={containerRef}
          style={{ flex: 1, minHeight: 0, minWidth: 0, overflow: "hidden" }}
        />
      </div>
    </div>
  );
}
