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
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Paper from "@mui/material/Paper";
import Checkbox from "@mui/material/Checkbox";
import Box from "@mui/material/Box";
import Link from "@mui/material/Link";
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import { Tag, Text, Button } from "@nvidia/foundations-react-core";
import { Terminal, TrendingUp } from "lucide-react";
import type { NodeIssueRollup } from "../../types/api";

/** Build https URL for BMC / RedFish UI; bracket bare IPv6 literals only. */
function redfishHttpsHref(host: string): string | null {
  const h = host.trim();
  if (!h || /[\s/@#?\\]/.test(h)) return null;
  if (h.startsWith("[")) return `https://${h}`;
  const colons = (h.match(/:/g) || []).length;
  if (colons >= 2 || h.includes("::")) return `https://[${h}]`;
  return `https://${h}`;
}

export interface NodeInfo {
  /** Selection and action target. For an inventory-backed rack this is the real hostname. */
  id: string;
  status: "up" | "down" | "inst" | "unknown";
  ip?: string;
  /** Resolvable compute hostname (Slurm/DNS), shown in its own column. Defaults to ``id``. */
  hostname?: string;
  /** Where the node sits in the rack, e.g. the cluster manager's ``U20:11``. Defaults to ``id``. */
  label?: string;
}

interface NodeSelectorProps {
  nodes: NodeInfo[];
  selected: Set<string>;
  onSelectionChange: (selected: Set<string>) => void;
  onOpenTerminal?: (nodeId: string) => void;
  issueNodes?: Record<string, NodeIssueRollup>;
  onOpenIssues?: (nodeId: string) => void;
}

function statusLabel(status: NodeInfo["status"]) {
  switch (status) {
    case "up": return "UP";
    case "down": return "DOWN";
    case "inst": return "INST";
    default: return "?";
  }
}

function RedfishIpCell({ ip }: { ip?: string }) {
  const trimmed = ip?.trim();
  if (!trimmed) {
    return <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>—</Text>;
  }
  const href = redfishHttpsHref(trimmed);
  if (!href) {
    return <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>{trimmed}</Text>;
  }
  return (
    <Link
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      underline="always"
      title={`RedFish BMC — ${href}`}
      onClick={(e) => e.stopPropagation()}
      sx={{
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
        fontSize: 13,
        fontWeight: 500,
        color: "primary.main",
        cursor: "pointer",
      }}
    >
      {trimmed}
    </Link>
  );
}

export function NodeSelector({ nodes, selected, onSelectionChange, onOpenTerminal, issueNodes, onOpenIssues }: NodeSelectorProps) {
  const allSelected = nodes.length > 0 && selected.size === nodes.length;
  const someSelected = selected.size > 0 && selected.size < nodes.length;

  const handleToggleAll = () => {
    if (allSelected) {
      onSelectionChange(new Set());
    } else {
      onSelectionChange(new Set(nodes.map((n) => n.id)));
    }
  };

  const handleToggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    onSelectionChange(next);
  };

  const selectDown = () => {
    onSelectionChange(new Set(nodes.filter((n) => n.status === "down").map((n) => n.id)));
  };

  const selectUp = () => {
    onSelectionChange(new Set(nodes.filter((n) => n.status === "up").map((n) => n.id)));
  };

  return (
    <Box>
      <Box sx={{ display: "flex", alignItems: "center", gap: 1, mb: 1.5 }}>
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          {selected.size} of {nodes.length} nodes selected
        </Text>
        <Button kind="secondary" size="small" onClick={handleToggleAll}>
          {allSelected ? "Clear all" : "Select all"}
        </Button>
        <Button kind="secondary" size="small" onClick={selectDown}>
          Down only
        </Button>
        <Button kind="secondary" size="small" onClick={selectUp}>
          Up only
        </Button>
      </Box>

      <TableContainer component={Paper} variant="outlined" sx={{ maxHeight: 360 }}>
        <Table size="small" stickyHeader>
          <TableHead>
            <TableRow>
              <TableCell padding="checkbox">
                <Checkbox
                  indeterminate={someSelected}
                  checked={allSelected}
                  onChange={handleToggleAll}
                />
              </TableCell>
              <TableCell>Node</TableCell>
              <TableCell>Hostname</TableCell>
              <TableCell>Status</TableCell>
              {(issueNodes || onOpenIssues) && <TableCell>Issue trend</TableCell>}
              <TableCell>RedFish IP</TableCell>
              {onOpenTerminal && <TableCell align="center">Actions</TableCell>}
            </TableRow>
          </TableHead>
          <TableBody>
            {nodes.map((node) => (
              <TableRow
                key={node.id}
                hover
                onClick={() => handleToggle(node.id)}
                sx={{ cursor: "pointer" }}
              >
                <TableCell padding="checkbox">
                  <Checkbox checked={selected.has(node.id)} />
                </TableCell>
                <TableCell>
                  <Text kind="body/regular/md" style={{ fontFamily: "monospace" }}>
                    {node.label ?? node.id}
                  </Text>
                </TableCell>
                <TableCell>
                  <Text kind="body/regular/sm" style={{ fontFamily: "monospace" }}>
                    {node.hostname ?? node.id}
                  </Text>
                </TableCell>
                <TableCell>
                  <Tag
                    color={node.status === "up" ? "green" : node.status === "down" ? "red" : node.status === "inst" ? "blue" : "gray"}
                    kind="solid"
                    readOnly
                    density="compact"
                  >
                    {statusLabel(node.status)}
                  </Tag>
                </TableCell>
                {(issueNodes || onOpenIssues) && (
                  <TableCell onClick={(e) => e.stopPropagation()}>
                    {issueNodes?.[node.id] && (
                      <Button
                        kind="secondary"
                        size="small"
                        onClick={() => onOpenIssues?.(node.id)}
                        title={`Open all issue history for ${node.id}`}
                      >
                        <TrendingUp size={14} />
                        ▲{issueNodes[node.id].issue_count}
                        {issueNodes[node.id].drained ? " ✖" : ""}
                        {issueNodes[node.id].nccl ? " ⚑" : ""}
                        {issueNodes[node.id].maintenance ? " ▦" : ""}
                      </Button>
                    )}
                  </TableCell>
                )}
                <TableCell onClick={(e) => e.stopPropagation()}>
                  <RedfishIpCell ip={node.ip} />
                </TableCell>
                {onOpenTerminal && (
                  <TableCell align="center" onClick={(e) => e.stopPropagation()}>
                    {node.status === "up" && (
                      <Tooltip title={`Open terminal to ${node.id}`}>
                        <IconButton size="small" onClick={() => onOpenTerminal(node.id)}>
                          <Terminal size={16} />
                        </IconButton>
                      </Tooltip>
                    )}
                  </TableCell>
                )}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
    </Box>
  );
}
