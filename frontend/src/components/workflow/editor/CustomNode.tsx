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
 * Custom node renderers for workflow steps.
 * Uses SVG polygon for diamond shape (matching mermaid-reactflow-editor).
 * Shows edge delete buttons on node hover near the connected handles.
 */

import { memo, useState } from "react";
import { Handle, Position, type NodeProps } from "reactflow";
import Box from "@mui/material/Box";
import { Text } from "@nvidia/foundations-react-core";
import { Play, Hand, GitBranch, Bot, CircleDot, Timer } from "lucide-react";

interface ConnectedEdge {
  edgeId: string;
  source: string;
  target: string;
  position: "top" | "bottom";
}

interface CustomNodeData {
  label: string;
  shape: string;
  stepType?: string;
  isActive?: boolean;
  isCompleted?: boolean;
  isFailed?: boolean;
  style?: Record<string, unknown>;
  connectedEdges?: ConnectedEdge[];
  onDeleteEdge?: (edgeId: string) => void;
}

const STEP_ICONS: Record<string, React.ReactNode> = {
  automated: <Play size={14} />,
  manual: <Hand size={14} />,
  decision: <GitBranch size={14} />,
  ai_query: <Bot size={14} />,
  delay: <Timer size={14} />,
};

const STEP_LABELS: Record<string, string> = {
  automated: "Action",
  manual: "Manual",
  decision: "Condition",
  ai_query: "AI",
  delay: "Delay",
};

function getColors(stepType: string, isActive?: boolean, isCompleted?: boolean, isFailed?: boolean) {
  let borderColor = "#76B900";
  let bgColor = "rgba(118,185,0,0.05)";
  let accentColor = "#76B900";

  if (stepType === "decision") {
    borderColor = "#f59e0b";
    bgColor = "rgba(245,158,11,0.06)";
    accentColor = "#f59e0b";
  } else if (stepType === "manual") {
    borderColor = "#3b82f6";
    bgColor = "rgba(59,130,246,0.06)";
    accentColor = "#3b82f6";
  } else if (stepType === "ai_query") {
    borderColor = "#8b5cf6";
    bgColor = "rgba(139,92,246,0.06)";
    accentColor = "#8b5cf6";
  }

  if (isActive) {
    borderColor = "#22c55e";
    bgColor = "rgba(34,197,94,0.12)";
    accentColor = "#22c55e";
  } else if (isCompleted) {
    borderColor = "#6b7280";
    bgColor = "rgba(107,114,128,0.06)";
    accentColor = "#6b7280";
  } else if (isFailed) {
    borderColor = "#ef4444";
    bgColor = "rgba(239,68,68,0.1)";
    accentColor = "#ef4444";
  }

  return { borderColor, bgColor, accentColor };
}

const handleStyle = (color: string, hovered: boolean, selected: boolean) => ({
  background: hovered || selected ? color : "transparent",
  border: `2px solid ${hovered || selected ? color : "transparent"}`,
  width: 12,
  height: 12,
  transition: "all 0.15s ease",
});

const deleteButtonStyle: React.CSSProperties = {
  width: 18,
  height: 18,
  borderRadius: "50%",
  background: "#ef4444",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  cursor: "pointer",
  color: "#fff",
  fontSize: 11,
  fontWeight: 700,
  lineHeight: 1,
  boxShadow: "0 1px 4px rgba(0,0,0,0.4)",
  userSelect: "none",
  zIndex: 10,
  position: "absolute",
};

function EdgeDeleteButtons({
  connectedEdges,
  onDeleteEdge,
  nodeId,
}: {
  connectedEdges: ConnectedEdge[];
  onDeleteEdge: (edgeId: string) => void;
  nodeId: string;
}) {
  const outgoing = connectedEdges.filter((e) => e.source === nodeId);
  const incoming = connectedEdges.filter((e) => e.target === nodeId);

  return (
    <>
      {/* Outgoing edges: delete buttons below the node (near bottom-source handle) */}
      {outgoing.map((edge, i) => (
        <div
          key={edge.edgeId}
          onClick={(e) => { e.stopPropagation(); onDeleteEdge(edge.edgeId); }}
          style={{
            ...deleteButtonStyle,
            left: `calc(50% + ${(i - (outgoing.length - 1) / 2) * 22}px)`,
            bottom: -24,
            transform: "translateX(-50%)",
          }}
          title={`Remove connection to ${edge.target}`}
        >
          ✕
        </div>
      ))}
      {/* Incoming edges: delete buttons above the node (near top-target handle) */}
      {incoming.map((edge, i) => (
        <div
          key={edge.edgeId}
          onClick={(e) => { e.stopPropagation(); onDeleteEdge(edge.edgeId); }}
          style={{
            ...deleteButtonStyle,
            left: `calc(50% + ${(i - (incoming.length - 1) / 2) * 22}px)`,
            top: -24,
            transform: "translateX(-50%)",
          }}
          title={`Remove connection from ${edge.source}`}
        >
          ✕
        </div>
      ))}
    </>
  );
}

// ---- Standard rectangular/rounded node ----

function StandardNode({ id, data, selected }: NodeProps<CustomNodeData>) {
  const { label, stepType = "automated", isActive, isCompleted, isFailed, connectedEdges, onDeleteEdge } = data;
  const [hovered, setHovered] = useState(false);
  const { borderColor, bgColor, accentColor } = getColors(stepType, isActive, isCompleted, isFailed);
  const hs = handleStyle(accentColor, hovered, !!selected);

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{ width: "100%", height: "100%", position: "relative" }}
    >
      <Handle type="target" position={Position.Top} id="top-target" style={{ ...hs, left: "50%", top: 0, transform: "translate(-50%,-50%)" }} />

      <Box
        sx={{
          width: "100%",
          height: "100%",
          borderRadius: stepType === "manual" || stepType === "ai_query" ? "16px" : "8px",
          border: `2px solid ${borderColor}`,
          bgcolor: bgColor,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 0.75,
          boxShadow: selected
            ? `0 0 0 3px ${accentColor}40`
            : isActive
              ? `0 0 12px ${accentColor}60`
              : hovered
                ? "0 2px 8px rgba(0,0,0,0.15)"
                : "0 1px 3px rgba(0,0,0,0.08)",
          transition: "all 0.2s ease",
          cursor: "grab",
          "&:active": { cursor: "grabbing" },
          px: 1.5,
        }}
      >
        <Box sx={{ color: accentColor, display: "flex", alignItems: "center", flexShrink: 0 }}>
          {STEP_ICONS[stepType] || <CircleDot size={14} />}
        </Box>
        <Text kind="body/regular/sm" style={{ lineHeight: 1.3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {label}
        </Text>
      </Box>

      {/* Badge */}
      {(hovered || selected) && (
        <div style={{
          position: "absolute", top: -10, left: "50%", transform: "translateX(-50%)",
          background: accentColor, color: "#fff", padding: "1px 6px", borderRadius: 4,
          fontSize: 10, fontWeight: 600, whiteSpace: "nowrap", zIndex: 1,
        }}>
          {STEP_LABELS[stepType] || "Step"}
        </div>
      )}

      {/* Edge delete buttons */}
      {hovered && connectedEdges && connectedEdges.length > 0 && onDeleteEdge && (
        <EdgeDeleteButtons connectedEdges={connectedEdges} onDeleteEdge={onDeleteEdge} nodeId={id} />
      )}

      <Handle type="source" position={Position.Bottom} id="bottom-source" style={{ ...hs, left: "50%", bottom: 0, transform: "translate(-50%,50%)" }} />
      <Handle type="target" position={Position.Left} id="left-target" style={{ ...hs, left: 0, top: "50%", transform: "translate(-50%,-50%)" }} />
      <Handle type="source" position={Position.Right} id="right-source" style={{ ...hs, right: 0, top: "50%", transform: "translate(50%,-50%)" }} />
    </div>
  );
}

// ---- Diamond/conditional node using SVG polygon ----

function DiamondNode({ id, data, selected }: NodeProps<CustomNodeData>) {
  const { label, stepType = "decision", isActive, isCompleted, isFailed, connectedEdges, onDeleteEdge } = data;
  const [hovered, setHovered] = useState(false);
  const { borderColor, bgColor, accentColor } = getColors(stepType, isActive, isCompleted, isFailed);
  const hs = handleStyle(accentColor, hovered, !!selected);

  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{ width: "100%", height: "100%", position: "relative" }}
    >
      {/* SVG diamond shape fills the rectangular container */}
      <svg
        width="100%"
        height="100%"
        viewBox="0 0 100 100"
        preserveAspectRatio="none"
        style={{ position: "absolute", inset: 0, pointerEvents: "none", filter: selected ? `drop-shadow(0 0 4px ${accentColor})` : isActive ? `drop-shadow(0 0 8px ${accentColor})` : "none" }}
      >
        <polygon
          points="50,5 97,50 50,95 3,50"
          fill={bgColor}
          stroke={borderColor}
          strokeWidth={1}
          strokeLinejoin="round"
        />
      </svg>

      {/* Text centered at the exact middle of the diamond */}
      <div style={{
        position: "absolute", inset: 0, display: "flex",
        alignItems: "center", justifyContent: "center",
        textAlign: "center", pointerEvents: "none",
        padding: "0 15%",
      }}>
        <div style={{
          fontSize: 12, fontWeight: 600, lineHeight: 1.2,
          overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
        }}>
          {label.replace(/^"|"$/g, "")}
        </div>
      </div>

      {/* Icon sits just above the text, anchored to the text's top edge */}
      <div style={{
        position: "absolute", left: "50%", top: "50%",
        transform: "translate(-50%, calc(-100% - 10px))",
        color: accentColor, display: "flex", alignItems: "center",
        pointerEvents: "none", opacity: 0.7,
      }}>
        <GitBranch size={11} />
      </div>

      {/* Badge */}
      {(hovered || selected) && (
        <div style={{
          position: "absolute", top: -10, left: "50%", transform: "translateX(-50%)",
          background: accentColor, color: "#fff", padding: "1px 6px", borderRadius: 4,
          fontSize: 10, fontWeight: 600, whiteSpace: "nowrap", zIndex: 1,
        }}>
          Condition
        </div>
      )}

      {/* Edge delete buttons */}
      {hovered && connectedEdges && connectedEdges.length > 0 && onDeleteEdge && (
        <EdgeDeleteButtons connectedEdges={connectedEdges} onDeleteEdge={onDeleteEdge} nodeId={id} />
      )}

      {/* Connection handles at midpoints of each side */}
      <Handle type="target" position={Position.Top} id="top-target" style={{ ...hs, left: "50%", top: 0, transform: "translate(-50%,-50%)" }} />
      <Handle type="source" position={Position.Bottom} id="bottom-source" style={{ ...hs, left: "50%", bottom: 0, transform: "translate(-50%,50%)" }} />
      <Handle type="target" position={Position.Left} id="left-target" style={{ ...hs, left: 0, top: "50%", transform: "translate(-50%,-50%)" }} />
      <Handle type="source" position={Position.Left} id="left-source" style={{ ...hs, left: 0, top: "50%", transform: "translate(-50%,-50%)" }} />
      <Handle type="target" position={Position.Right} id="right-target" style={{ ...hs, right: 0, top: "50%", transform: "translate(50%,-50%)" }} />
      <Handle type="source" position={Position.Right} id="right-source" style={{ ...hs, right: 0, top: "50%", transform: "translate(50%,-50%)" }} />
    </div>
  );
}

export const CustomNodeMemo = memo(StandardNode);
export const DiamondNodeMemo = memo(DiamondNode);

export default CustomNodeMemo;
