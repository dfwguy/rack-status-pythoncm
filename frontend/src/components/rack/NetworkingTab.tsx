import Box from "@mui/material/Box";
import { ActionCard } from "./ActionCard";
import { Network, RotateCcw, ShieldCheck, ScanSearch, Map, Radio } from "lucide-react";

interface NetworkingTabProps {
  rack: string;
  selectedNodes: Set<string>;
  disabled: boolean;
}

export function NetworkingTab({ rack, selectedNodes, disabled }: NetworkingTabProps) {
  const nodeCount = selectedNodes.size;

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <ActionCard
        title="NVSwitch Fix"
        description={`Run NVSwitch recovery on ${nodeCount} selected node(s). Attempts to restore degraded NVSwitch links.`}
        icon={<Network size={20} />}
        buttonLabel="Fix"
        disabled={disabled}
        selectedNodes={selectedNodes}
      />
      <ActionCard
        title="NVSwitch SDN Reset"
        description={`Reset NVSwitch SDN controller for ${nodeCount} selected node(s). Reinitializes software-defined networking.`}
        icon={<RotateCcw size={20} />}
        buttonLabel="SDN Reset"
        disabled={disabled}
        selectedNodes={selectedNodes}
      />
      <ActionCard
        title="NVSwitch System Reset"
        description={`Full NVLink system reset for ${rack}. Resets all NVSwitch/NVLink connections at hardware level.`}
        icon={<Radio size={20} />}
        buttonLabel="System Reset"
        destructive
        disabled={disabled}
        selectedNodes={selectedNodes}
      />
      <ActionCard
        title="NVSwitch Audit"
        description={`Stream live NVSwitch audit for ${nodeCount} selected node(s). Checks link health and error counters.`}
        icon={<ShieldCheck size={20} />}
        buttonLabel="Audit"
        disabled={disabled}
        selectedNodes={selectedNodes}
      />
      <ActionCard
        title="NVSwitch Check"
        description={`Quick NVSwitch health check for ${nodeCount} selected node(s). Reports degraded links.`}
        icon={<ScanSearch size={20} />}
        buttonLabel="Check"
        disabled={disabled}
        selectedNodes={selectedNodes}
      />
      <ActionCard
        title="NVLMapper"
        description={`Run NVLMapper topology analysis on ${rack}. Maps NVLink/NVSwitch connectivity.`}
        icon={<Map size={20} />}
        buttonLabel="Run NVLMapper"
        disabled={disabled}
        selectedNodes={selectedNodes}
      />
    </Box>
  );
}
