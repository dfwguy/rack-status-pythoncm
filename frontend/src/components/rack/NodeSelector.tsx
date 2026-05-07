import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Paper from "@mui/material/Paper";
import Checkbox from "@mui/material/Checkbox";
import Box from "@mui/material/Box";
import { Tag, Text, Button } from "@nvidia/foundations-react-core";

export interface NodeInfo {
  id: string;
  status: "up" | "down" | "inst" | "unknown";
  ip?: string;
}

interface NodeSelectorProps {
  nodes: NodeInfo[];
  selected: Set<string>;
  onSelectionChange: (selected: Set<string>) => void;
}

function statusLabel(status: NodeInfo["status"]) {
  switch (status) {
    case "up": return "UP";
    case "down": return "DOWN";
    case "inst": return "INST";
    default: return "?";
  }
}

export function NodeSelector({ nodes, selected, onSelectionChange }: NodeSelectorProps) {
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
              <TableCell>IP</TableCell>
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
                <TableCell>
                  <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                    {node.ip || "—"}
                  </Text>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
    </Box>
  );
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
): NodeInfo[] {
  const nodes: Map<string, NodeInfo> = new Map();
  const rackLower = rack.toLowerCase();

  // Parse segments from ct_down_unk
  const segments = ctDownUnk.split(";").map((s) => s.trim()).filter(Boolean);
  for (const seg of segments) {
    let status: NodeInfo["status"] = "unknown";
    if (seg.startsWith("DN:")) status = "down";
    else if (seg.startsWith("UP:")) status = "up";
    else if (seg.startsWith("INST:")) status = "inst";

    const bracketMatch = seg.match(/ct\[([^\]]+)\]/i);
    if (bracketMatch) {
      const inner = bracketMatch[1];
      for (const num of expandRange(inner)) {
        const id = `${rackLower}-ct${String(num).padStart(2, "0")}`;
        const ip = physicalRf0[id] || "";
        nodes.set(id, { id, status, ip });
      }
    }
  }

  // Fill in any missing nodes from ctCount range
  for (let i = 1; i <= ctCount; i++) {
    const id = `${rackLower}-ct${String(i).padStart(2, "0")}`;
    if (!nodes.has(id)) {
      const ip = physicalRf0[id] || "";
      nodes.set(id, { id, status: "unknown", ip });
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
