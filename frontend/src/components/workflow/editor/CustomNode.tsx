/**
 * Custom node renderer for workflow steps.
 * Supports: automated (rectangle), manual (rounded), decision (diamond), ai_query (rounded purple).
 * Adapted from mermaid-reactflow-editor (MIT License).
 */

import { memo, useState } from "react";
import { Handle, Position, type NodeProps } from "reactflow";
import Box from "@mui/material/Box";
import { Text } from "@nvidia/foundations-react-core";
import { Play, Hand, GitBranch, Bot, CircleDot } from "lucide-react";

interface CustomNodeData {
  label: string;
  shape: string;
  stepType?: string;
  isActive?: boolean;
  isCompleted?: boolean;
  isFailed?: boolean;
  style?: Record<string, unknown>;
}

const STEP_ICONS: Record<string, React.ReactNode> = {
  automated: <Play size={14} />,
  manual: <Hand size={14} />,
  decision: <GitBranch size={14} />,
  ai_query: <Bot size={14} />,
};

const STEP_LABELS: Record<string, string> = {
  automated: "Action",
  manual: "Manual",
  decision: "Condition",
  ai_query: "AI",
};

function CustomNode({ data, selected }: NodeProps<CustomNodeData>) {
  const { label, stepType = "automated", isActive, isCompleted, isFailed } = data;
  const [hovered, setHovered] = useState(false);

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

  const handleStyle = {
    background: hovered || selected ? accentColor : "transparent",
    border: `2px solid ${accentColor}`,
    width: 10,
    height: 10,
    transition: "all 0.15s ease",
  };

  const isDiamond = stepType === "decision";

  return (
    <Box
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      sx={{ position: "relative" }}
    >
      <Handle type="target" position={Position.Top} style={{ ...handleStyle, top: isDiamond ? -4 : -5 }} />

      <Box
        sx={{
          px: 2.5,
          py: 1.5,
          borderRadius: isDiamond ? "4px" : stepType === "manual" || stepType === "ai_query" ? "16px" : "8px",
          border: `2px solid ${borderColor}`,
          bgcolor: bgColor,
          minWidth: 150,
          textAlign: "center",
          boxShadow: selected
            ? `0 0 0 3px ${accentColor}40`
            : isActive
              ? `0 0 12px ${accentColor}60`
              : hovered
                ? `0 2px 8px rgba(0,0,0,0.15)`
                : "0 1px 3px rgba(0,0,0,0.08)",
          transform: isDiamond ? "rotate(0deg)" : undefined,
          transition: "all 0.2s ease",
          cursor: "grab",
          "&:active": { cursor: "grabbing" },
        }}
      >
        {/* Type badge */}
        <Box
          sx={{
            position: "absolute",
            top: -10,
            left: "50%",
            transform: "translateX(-50%)",
            bgcolor: accentColor,
            color: "#fff",
            px: 0.75,
            py: 0.1,
            borderRadius: "4px",
            fontSize: 10,
            fontWeight: 600,
            letterSpacing: "0.02em",
            lineHeight: 1.4,
            opacity: hovered || selected ? 1 : 0,
            transition: "opacity 0.15s ease",
            whiteSpace: "nowrap",
          }}
        >
          {STEP_LABELS[stepType] || "Step"}
        </Box>

        <Box sx={{ display: "flex", alignItems: "center", gap: 1, justifyContent: "center" }}>
          <Box sx={{ color: accentColor, display: "flex", alignItems: "center", flexShrink: 0 }}>
            {STEP_ICONS[stepType] || <CircleDot size={14} />}
          </Box>
          <Text kind="body/regular/sm" style={{ lineHeight: 1.3, maxWidth: 160, overflow: "hidden", textOverflow: "ellipsis" }}>
            {label}
          </Text>
        </Box>
      </Box>

      <Handle type="source" position={Position.Bottom} style={{ ...handleStyle, bottom: isDiamond ? -4 : -5 }} />

      {/* Extra handles on sides for decision nodes (Yes/No branches) */}
      {isDiamond && (
        <>
          <Handle
            type="source"
            position={Position.Left}
            id="left"
            style={{ ...handleStyle, left: -5, top: "50%" }}
          />
          <Handle
            type="source"
            position={Position.Right}
            id="right"
            style={{ ...handleStyle, right: -5, top: "50%" }}
          />
        </>
      )}
    </Box>
  );
}

export default memo(CustomNode);
