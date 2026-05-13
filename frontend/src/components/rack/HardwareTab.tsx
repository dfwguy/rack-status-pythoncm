import { useState, useMemo } from "react";
import Box from "@mui/material/Box";
import { ActionCard } from "./ActionCard";
import { MacChangeDialog } from "./MacChangeDialog";
import { Power, Cpu, ScanSearch, Bug, RotateCcw, CircleCheck, Zap, Network } from "lucide-react";
import { useRackCommands } from "../../context/RackCommandContext";
import { attachLogDir, bmcRecoveryBody, specsCommandBody } from "../../lib/rackCommandBodies";
interface HardwareTabProps {
  rack: string;
  logDir?: string;
  selectedNodes: Set<string>;
  disabled: boolean;
  /** Pre-select down trays before Power On confirm (legacy power dialog defaults). */
  onSelectPowerResetTargets: () => void;
}

export function HardwareTab({ rack, logDir, selectedNodes, disabled, onSelectPowerResetTargets }: HardwareTabProps) {
  const { runAsyncReset } = useRackCommands();
  const nodeCount = selectedNodes.size;
  const [macDialogOpen, setMacDialogOpen] = useState(false);

  const specsBody = () => specsCommandBody(rack, logDir, selectedNodes);
  const imexBody = () => attachLogDir({ rack }, logDir);

  const singleNode = useMemo(() => {
    if (selectedNodes.size !== 1) return null;
    const [only] = [...selectedNodes];
    return only ?? null;
  }, [selectedNodes]);

  const macChangeDescription =
    singleNode
      ? `Change MAC(s) on ${singleNode}. Auto-swaps if the new MAC is owned by exactly one other node.`
      : selectedNodes.size === 0
        ? "Select exactly 1 node to enable."
        : `Select exactly 1 node (currently ${selectedNodes.size}).`;

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <ActionCard
        title="Power On"
        description={`Power on ${nodeCount} selected node(s) in ${rack} via cmsh device power cycle.`}
        icon={<Power size={20} />}
        buttonLabel="Power On"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onBeforeConfirmDialog={onSelectPowerResetTargets}
        onRun={() => runAsyncReset("Power on", "power", "/api/power-on-async", specsBody())}
      />
      <ActionCard
        title="GPU Reset"
        description={`Reset GPUs on ${nodeCount} selected node(s). Stops slurmd, runs nvidia-smi -r, restarts services.`}
        icon={<Cpu size={20} />}
        buttonLabel="Reset GPUs"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() => runAsyncReset("GPU reset", "gpu", "/api/gpu-reset-async", specsBody())}
      />
      <ActionCard
        title="Hardware Check"
        description={`Run hardware diagnostic (checker.sh) on ${nodeCount} selected node(s) via pdsh.`}
        icon={<ScanSearch size={20} />}
        buttonLabel="Run Check"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() =>
          runAsyncReset("Hardware check", "hw", "/api/hw-check-async", { ...specsBody(), preset: "default" })
        }
      />
      <ActionCard
        title="Bug Report"
        description={`Collect nvidia-bug-report.sh from ${nodeCount} selected node(s). Archives stored on dashboard host.`}
        icon={<Bug size={20} />}
        buttonLabel="Collect"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() => runAsyncReset("Bug report", "hw", "/api/bug-report-async", specsBody())}
      />
      <ActionCard
        title="IMEX Reset"
        description={`Reset IMEX daemon on ${rack} (rack-level script).`}
        icon={<RotateCcw size={20} />}
        buttonLabel="Reset IMEX"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() => runAsyncReset("IMEX reset", "nmxc", "/api/imex-reset-async", imexBody())}
      />
      <ActionCard
        title="IMEX Check"
        description={`Verify IMEX daemon health on ${rack}.`}
        icon={<CircleCheck size={20} />}
        buttonLabel="Check IMEX"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() => runAsyncReset("IMEX check", "hw", "/api/imex-check-async", imexBody())}
      />
      <ActionCard
        title="BMC Recovery"
        description={`Cold-reset the BMC (rf0) on ${nodeCount} selected node(s) via ipmitool.`}
        icon={<Zap size={20} />}
        buttonLabel="Reset BMC"
        disabled={disabled}
        destructive
        selectedNodes={selectedNodes}
        onRun={() =>
          runAsyncReset("BMC recovery", "bmc", "/api/bmc-recovery-async", bmcRecoveryBody(rack, logDir, selectedNodes))
        }
      />
      <ActionCard
        title="MAC Change"
        description={macChangeDescription}
        icon={<Network size={20} />}
        buttonLabel="Change MAC"
        disabled={disabled || !singleNode}
        onRun={() => setMacDialogOpen(true)}
      />
      {singleNode && (
        <MacChangeDialog
          open={macDialogOpen}
          rack={rack}
          node={singleNode}
          onClose={() => setMacDialogOpen(false)}
        />
      )}
    </Box>
  );
}
