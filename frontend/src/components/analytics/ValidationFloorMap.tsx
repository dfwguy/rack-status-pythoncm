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
import Box from "@mui/material/Box";
import { Text } from "@nvidia/foundations-react-core";
import type { PostRunNode } from "../../types/api";
import { HEALTH_COLOR } from "../../lib/rackTopology";
import { naturalCompare, type NodeValidationState, type SuGroup, type ValidationStatus } from "../../lib/validationStatus";

export interface FloorNode {
  node: PostRunNode;
  state: NodeValidationState;
}

interface Props {
  groups: SuGroup<FloorNode>[];
  onNode: (node: PostRunNode) => void;
}

const STATUS_COLOR: Record<ValidationStatus, string> = {
  pass: HEALTH_COLOR.healthy,
  attention: HEALTH_COLOR.down,
  warning: HEALTH_COLOR.partial,
  running: "var(--color-blue-500, #1565c0)",
  not_run: HEALTH_COLOR.partial,
  unknown: HEALTH_COLOR.unknown,
};

const STATUS_PRIORITY: ValidationStatus[] = ["attention", "running", "warning", "not_run", "unknown", "pass"];

function rackLabel(rack: string): string {
  return rack.match(/(\d+)$/)?.[1] || rack.split("-").at(-1) || rack;
}

function rackStatus(nodes: FloorNode[]): ValidationStatus {
  return STATUS_PRIORITY.find((status) => nodes.some((entry) => entry.state.status === status)) || "unknown";
}

function ValidationRackTile({ rack, onNode }: { rack: { rack: string; nodes: FloorNode[] }; onNode: Props["onNode"] }) {
  const nodes = [...rack.nodes].sort((a, b) => naturalCompare(a.node.node, b.node.node)).slice(0, 18);
  const status = rackStatus(nodes);
  const color = STATUS_COLOR[status];
  const passed = nodes.filter((entry) => entry.state.status === "pass").length;
  const selected = STATUS_PRIORITY
    .map((priority) => nodes.find((entry) => entry.state.status === priority))
    .find(Boolean) || nodes[0];

  return (
    <Box
      role="button"
      tabIndex={0}
      aria-label={`Rack ${rack.rack}: ${status.replace("_", " ")}, ${passed}/${nodes.length} passed`}
      onClick={() => selected && onNode(selected.node)}
      onKeyDown={(event) => {
        if ((event.key === "Enter" || event.key === " ") && selected) {
          event.preventDefault();
          onNode(selected.node);
        }
      }}
      sx={{
        width: 96,
        cursor: "pointer",
        border: "1px solid var(--border-color-base, rgba(0,0,0,0.18))",
        borderLeft: `4px solid ${color}`,
        borderRadius: "4px",
        p: 0.75,
        backgroundColor: "var(--background-color-surface, transparent)",
        transition: "box-shadow 120ms, transform 120ms",
        "&:hover": { boxShadow: "0 2px 8px rgba(0,0,0,0.18)", transform: "translateY(-1px)" },
        "&:focus-visible": { outline: `2px solid ${color}`, outlineOffset: 1 },
      }}
    >
      <Box sx={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 0.5 }}>
        <Text kind="mono/sm" style={{ fontWeight: 700 }}>{rackLabel(rack.rack)}</Text>
        <Box sx={{ width: 8, height: 8, borderRadius: "50%", backgroundColor: color }} />
      </Box>
      <Box sx={{ display: "grid", gridTemplateColumns: "repeat(9, 1fr)", gap: "2px", mt: 0.5 }}>
        {nodes.map((entry) => (
          <Box
            key={entry.node.node}
            title={entry.state.tooltip}
            sx={{
              minWidth: 0,
              height: 9,
              borderRadius: "1px",
              backgroundColor: STATUS_COLOR[entry.state.status],
            }}
          />
        ))}
      </Box>
      <Text kind="body/regular/sm" style={{ display: "block", marginTop: 4, fontSize: 11, color: "var(--color-text-secondary, #6b6b6b)" }}>
        {passed}/{nodes.length} passed
      </Text>
    </Box>
  );
}

/** Validation status rendered with the same responsive rack-tile language as Compute > Racks. */
export function ValidationFloorMap({ groups, onNode }: Props) {
  return (
    <Box aria-label="Validation floor map" sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      {groups.map((group) => (
        <Box key={group.id} component="section" aria-label={`${group.id} ${group.rowKey}`} sx={{ display: "flex", alignItems: "flex-start", gap: 1.5 }}>
          <Box sx={{ minWidth: 64, pt: 1 }}>
            <Text kind="body/bold/sm" style={{ whiteSpace: "nowrap" }}>{group.id}</Text>
          </Box>
          <Box sx={{ display: "flex", flexWrap: "wrap", gap: 1 }}>
            {group.racks.map((rack) => <ValidationRackTile key={rack.rack} rack={rack} onNode={onNode} />)}
          </Box>
        </Box>
      ))}
    </Box>
  );
}
