import Box from "@mui/material/Box";
import { ActionCard } from "./ActionCard";
import { Power, Cpu, ScanSearch, Bug, RotateCcw, CircleCheck } from "lucide-react";

interface HardwareTabProps {
  rack: string;
  selectedNodes: Set<string>;
  disabled: boolean;
}

export function HardwareTab({ rack, selectedNodes, disabled }: HardwareTabProps) {
  const nodeCount = selectedNodes.size;

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <ActionCard
        title="Power On"
        description={`Power on ${nodeCount} selected node(s) in ${rack} via cmsh device power cycle.`}
        icon={<Power size={20} />}
        buttonLabel="Power On"
        disabled={disabled}
        selectedNodes={selectedNodes}
      />
      <ActionCard
        title="GPU Reset"
        description={`Reset GPUs on ${nodeCount} selected node(s). Stops slurmd, runs nvidia-smi -r, restarts services.`}
        icon={<Cpu size={20} />}
        buttonLabel="Reset GPUs"
        disabled={disabled}
        selectedNodes={selectedNodes}
      />
      <ActionCard
        title="Hardware Check"
        description={`Run hardware diagnostic (checker.sh) on ${nodeCount} selected node(s) via pdsh.`}
        icon={<ScanSearch size={20} />}
        buttonLabel="Run Check"
        disabled={disabled}
        selectedNodes={selectedNodes}
      />
      <ActionCard
        title="Bug Report"
        description={`Collect nvidia-bug-report.sh from ${nodeCount} selected node(s). Archives stored on dashboard host.`}
        icon={<Bug size={20} />}
        buttonLabel="Collect"
        disabled={disabled}
        selectedNodes={selectedNodes}
      />
      <ActionCard
        title="IMEX Reset"
        description={`Reset IMEX daemon on ${nodeCount} selected node(s). Restarts nvidia-imex service.`}
        icon={<RotateCcw size={20} />}
        buttonLabel="Reset IMEX"
        disabled={disabled}
        selectedNodes={selectedNodes}
      />
      <ActionCard
        title="IMEX Check"
        description={`Verify IMEX daemon health on ${nodeCount} selected node(s).`}
        icon={<CircleCheck size={20} />}
        buttonLabel="Check IMEX"
        disabled={disabled}
        selectedNodes={selectedNodes}
      />
    </Box>
  );
}
