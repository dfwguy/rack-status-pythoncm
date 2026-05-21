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
import { Terminal } from "lucide-react";
import type { NvisNamingSchema } from "../../types/naming";

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
  id: string;
  status: "up" | "down" | "inst" | "unknown";
  ip?: string;
}

interface NodeSelectorProps {
  nodes: NodeInfo[];
  selected: Set<string>;
  onSelectionChange: (selected: Set<string>) => void;
  onOpenTerminal?: (nodeId: string) => void;
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

export function NodeSelector({ nodes, selected, onSelectionChange, onOpenTerminal }: NodeSelectorProps) {
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
        <Button kind="tertiary" size="small" onClick={handleToggleAll}>
          {allSelected ? "Clear all" : "Select all"}
        </Button>
        <Button kind="tertiary" size="small" onClick={selectDown}>
          Down only
        </Button>
        <Button kind="tertiary" size="small" onClick={selectUp}>
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
              <TableCell>Status</TableCell>
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
                    {node.id}
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

function nodeIdForTray(
  rack: string,
  num: number,
  physicalRf0: Record<string, string>,
  naming?: NvisNamingSchema,
): string {
  const pad = String(num).padStart(2, "0");
  const joiner = naming?.ct_joiner ?? "-ct";
  const rk = rack.trim();
  const rkKey = rk.toLowerCase();
  if (naming?.node_id_mode === "full_hostname") {
    const suffix = pad;
    const hit = Object.keys(physicalRf0).find(
      (h) => h.toLowerCase().startsWith(rkKey) && h.toLowerCase().endsWith(suffix),
    );
    if (hit) return hit;
    return `${rk}${suffix}`;
  }
  return `${rk}${joiner}${pad}`;
}

/**
 * Parse the ct_down_unk string (e.g. "DN:p5-r01-ct[01-02];UP:p5-r01-ct[03-08]")
 * and physical_rf0 map into a list of NodeInfo objects.
 */
export function parseNodesFromRack(
  rack: string,
  ctDownUnk: string,
  physicalRf0: Record<string, string>,
  ctCount = 8,
  naming?: NvisNamingSchema,
): NodeInfo[] {
  const nodes: Map<string, NodeInfo> = new Map();
  const rackCanon = rack.trim();
  const rackKey = rackCanon.toLowerCase();
  const joiner = naming?.ct_joiner ?? "-ct";
  const bracketRe = joiner
    ? new RegExp(`${joiner.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\[([^\\]]+)\\]`, "i")
    : /\[([^\]]+)\]/i;

  const rfIp = (id: string): string => {
    if (physicalRf0[id] !== undefined) return physicalRf0[id] || "";
    const hit = Object.keys(physicalRf0).find((k) => k.toLowerCase() === id.toLowerCase());
    return hit ? physicalRf0[hit] || "" : "";
  };

  const segments = ctDownUnk.split(";").map((s) => s.trim()).filter(Boolean);
  for (const seg of segments) {
    let status: NodeInfo["status"] = "unknown";
    if (seg.startsWith("DN:")) status = "down";
    else if (seg.startsWith("UP:")) status = "up";
    else if (seg.startsWith("INST:")) status = "inst";

    const bracketMatch = seg.match(bracketRe);
    if (bracketMatch) {
      const inner = bracketMatch[1];
      for (const num of expandRange(inner)) {
        const id = nodeIdForTray(rackCanon, num, physicalRf0, naming);
        const ip = rfIp(id);
        nodes.set(id.toLowerCase(), { id, status, ip });
      }
    }
  }

  if (naming?.node_id_mode === "full_hostname") {
    for (const host of Object.keys(physicalRf0)) {
      const hKey = host.toLowerCase();
      if (hKey.startsWith(rackKey) && host.length > rackCanon.length && !nodes.has(hKey)) {
        nodes.set(hKey, { id: host, status: "unknown", ip: physicalRf0[host] || "" });
      }
    }
  } else {
    for (let i = 1; i <= ctCount; i++) {
      const id = nodeIdForTray(rackCanon, i, physicalRf0, naming);
      const k = id.toLowerCase();
      if (!nodes.has(k)) {
        const ip = rfIp(id);
        nodes.set(k, { id, status: "unknown", ip });
      }
    }
  }

  return [...nodes.values()].sort((a, b) => a.id.localeCompare(b.id, undefined, { numeric: true }));
}

function expandRange(inner: string): number[] {
  const nums: number[] = [];
  for (const part of inner.split(",")) {
    const trimmed = part.trim();
    if (trimmed.includes("-")) {
      const [startStr, endStr] = trimmed.split("-");
      const start = parseInt(startStr, 10);
      const end = parseInt(endStr, 10);
      if (!isNaN(start) && !isNaN(end)) {
        for (let i = start; i <= end; i++) nums.push(i);
      }
    } else {
      const n = parseInt(trimmed, 10);
      if (!isNaN(n)) nums.push(n);
    }
  }
  return nums;
}
