import Box from "@mui/material/Box";
import IconButton from "@mui/material/IconButton";
import TextField from "@mui/material/TextField";
import InputAdornment from "@mui/material/InputAdornment";
import { useMemo, useState } from "react";
import { Checkbox, Flex, Text, Button } from "@nvidia/foundations-react-core";
import { Boxes, Server, Cpu, ChevronRight, ChevronDown, PowerOff, Search } from "lucide-react";
import {
  type ScopeCluster,
  allAvailableHostIds,
} from "./mockData";

type CheckState = boolean | "indeterminate";

/** Roll-up state for a parent given the selected set and its selectable children. */
function rollup(selected: Set<string>, childIds: string[]): CheckState {
  if (childIds.length === 0) return false;
  const picked = childIds.filter((id) => selected.has(id)).length;
  if (picked === 0) return false;
  if (picked === childIds.length) return true;
  return "indeterminate";
}

interface ScopeTreeProps {
  topology: ScopeCluster;
  selected: Set<string>;
  onSelectedChange: (next: Set<string>) => void;
}

/**
 * Reusable cluster → rack → node selector. Checkboxes at every level with
 * indeterminate roll-up; unavailable nodes are disabled. Drives any view that
 * needs to scope by a set of hosts (Observability today, reusable elsewhere).
 */
export function ScopeTree({ topology, selected, onSelectedChange }: ScopeTreeProps) {
  const allRackIds = topology.racks.map((r) => r.id);
  const [openIds, setOpenIds] = useState<Set<string>>(() => new Set([topology.id, ...allRackIds]));
  const [query, setQuery] = useState("");

  const allHosts = allAvailableHostIds(topology);
  const q = query.trim().toLowerCase();

  /** Racks (and their visible nodes) after applying the search filter. */
  const filteredRacks = useMemo(() => {
    return topology.racks
      .map((rack) => {
        const rackMatches = rack.label.toLowerCase().includes(q) || rack.id.toLowerCase().includes(q);
        const nodes =
          q === "" || rackMatches
            ? rack.nodes
            : rack.nodes.filter((n) => n.label.toLowerCase().includes(q) || n.id.toLowerCase().includes(q));
        return { rack, nodes, visible: q === "" || rackMatches || nodes.length > 0 };
      })
      .filter((r) => r.visible);
  }, [topology.racks, q]);

  const searching = q !== "";

  const toggleOpen = (id: string) =>
    setOpenIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const setMany = (ids: string[], on: boolean) => {
    const next = new Set(selected);
    ids.forEach((id) => (on ? next.add(id) : next.delete(id)));
    onSelectedChange(next);
  };

  const clusterState = rollup(selected, allHosts);
  const clusterOpen = searching || openIds.has(topology.id);

  return (
    <Box>
      <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 8, color: "var(--text-color-secondary)" }}>
        Scope
      </Text>

      <TextField
        size="small"
        fullWidth
        placeholder="Search rack or node…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        slotProps={{
          input: {
            startAdornment: (
              <InputAdornment position="start">
                <Search size={16} />
              </InputAdornment>
            ),
          },
        }}
        sx={{ mb: 1 }}
      />

      <Box sx={{ display: "flex", flexDirection: "column", gap: 1, mb: 1.5 }}>
        <Flex gap="2">
          <Button kind="primary" size="small" style={{ flex: 1 }} onClick={() => onSelectedChange(new Set(allHosts))}>
            Select all
          </Button>
          <Button kind="primary" size="small" style={{ flex: 1 }} onClick={() => onSelectedChange(new Set())}>
            Clear
          </Button>
        </Flex>
        <Flex gap="2">
          <Button
            kind="primary"
            size="small"
            style={{ flex: 1 }}
            onClick={() => setOpenIds(new Set([topology.id, ...allRackIds]))}
          >
            Expand all
          </Button>
          <Button kind="primary" size="small" style={{ flex: 1 }} onClick={() => setOpenIds(new Set([topology.id]))}>
            Collapse all
          </Button>
        </Flex>
      </Box>

      {/* Cluster row */}
      <ScopeRow
        depth={0}
        open={clusterOpen}
        onToggleOpen={() => toggleOpen(topology.id)}
        icon={<Boxes size={16} />}
        label={topology.label}
        checked={clusterState}
        onCheckedChange={(c) => setMany(allHosts, c === true)}
      />

      {clusterOpen && filteredRacks.length === 0 && (
        <Box sx={{ pl: 3, py: 1 }}>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            No racks or nodes match “{query}”.
          </Text>
        </Box>
      )}

      {clusterOpen &&
        filteredRacks.map(({ rack, nodes }) => {
          const rackHosts = rack.nodes.filter((n) => n.available).map((n) => n.id);
          const rackOpen = searching || openIds.has(rack.id);
          return (
            <Box key={rack.id}>
              <ScopeRow
                depth={1}
                open={rackOpen}
                onToggleOpen={() => toggleOpen(rack.id)}
                icon={<Server size={16} />}
                label={rack.label}
                checked={rollup(selected, rackHosts)}
                disabled={rackHosts.length === 0}
                onCheckedChange={(c) => setMany(rackHosts, c === true)}
              />
              {rackOpen &&
                nodes.map((node) => (
                  <ScopeRow
                    key={node.id}
                    depth={2}
                    icon={node.available ? <Cpu size={16} /> : <PowerOff size={16} />}
                    label={node.label}
                    hint={node.available ? undefined : "unavailable"}
                    disabled={!node.available}
                    checked={selected.has(node.id)}
                    onCheckedChange={(c) => setMany([node.id], c === true)}
                  />
                ))}
            </Box>
          );
        })}
    </Box>
  );
}

interface ScopeRowProps {
  depth: number;
  icon: React.ReactNode;
  label: string;
  checked: CheckState;
  onCheckedChange: (checked: CheckState) => void;
  disabled?: boolean;
  hint?: string;
  open?: boolean;
  onToggleOpen?: () => void;
}

function ScopeRow({ depth, icon, label, checked, onCheckedChange, disabled, hint, open, onToggleOpen }: ScopeRowProps) {
  const isBranch = typeof open === "boolean" && !!onToggleOpen;
  return (
    <Flex
      align="center"
      gap="1"
      style={{ paddingLeft: depth * 18, minHeight: 34 }}
    >
      {isBranch ? (
        <IconButton size="small" onClick={onToggleOpen} sx={{ p: 0.25, color: "text.secondary" }} aria-label={open ? "Collapse" : "Expand"}>
          {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
        </IconButton>
      ) : (
        <Box sx={{ width: 24, flexShrink: 0 }} />
      )}
      <Checkbox
        checked={checked}
        disabled={disabled}
        onCheckedChange={onCheckedChange}
        slotLabel={
          <Flex align="center" gap="2" style={{ opacity: disabled ? 0.5 : 1 }}>
            <span style={{ display: "inline-flex", color: "var(--text-color-secondary)" }}>{icon}</span>
            <Text kind="body/regular/sm">{label}</Text>
            {hint && (
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                ({hint})
              </Text>
            )}
          </Flex>
        }
      />
    </Flex>
  );
}
