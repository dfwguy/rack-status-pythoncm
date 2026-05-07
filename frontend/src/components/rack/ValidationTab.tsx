import Box from "@mui/material/Box";
import { Text, Divider } from "@nvidia/foundations-react-core";
import { ActionCard } from "./ActionCard";
import { Play, Flame, Zap, FileSearch, XCircle, ArrowDownToLine, ArrowUpFromLine } from "lucide-react";

interface ValidationTabProps {
  rack: string;
  selectedNodes: Set<string>;
  disabled: boolean;
  jobId?: string;
  onViewResults?: () => void;
}

export function ValidationTab({ rack, selectedNodes, disabled, onViewResults }: ValidationTabProps) {
  const nodeCount = selectedNodes.size;

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <Text kind="body/bold/sm" style={{ color: "var(--text-color-secondary)", marginTop: 8 }}>
        Benchmarks &amp; Tests
      </Text>

      <ActionCard
        title="Run Validation (NEMO)"
        description={`Launch single-rack NEMO/NCCL validation on ${nodeCount} selected node(s) in ${rack}. Runs the configured NEMO validation script via Slurm.`}
        icon={<Play size={20} />}
        buttonLabel="Run NEMO"
        disabled={disabled}
        selectedNodes={selectedNodes}
      />
      <ActionCard
        title="Run Llama"
        description={`Launch Llama inference benchmark on ${nodeCount} selected node(s) in ${rack}. Submits a Slurm job using the Llama validation script.`}
        icon={<Flame size={20} />}
        buttonLabel="Run Llama"
        disabled={disabled}
        selectedNodes={selectedNodes}
      />
      <ActionCard
        title="Run Stress"
        description={`Launch MNUBERGEMM stress test on ${nodeCount} selected node(s) in ${rack}. Sustained GPU compute load for burn-in and thermal validation.`}
        icon={<Zap size={20} />}
        buttonLabel="Run Stress"
        disabled={disabled}
        selectedNodes={selectedNodes}
      />

      <div style={{ margin: "8px 0" }}><Divider /></div>
      <Text kind="body/bold/sm" style={{ color: "var(--text-color-secondary)" }}>
        Results &amp; Controls
      </Text>

      <ActionCard
        title="View Parser Results"
        description="Parse the latest validation log and show NCCL bandwidth summary, step-time charts, and key metrics."
        icon={<FileSearch size={20} />}
        buttonLabel="View"
        onRun={onViewResults}
      />
      <ActionCard
        title="Cancel Running Job"
        description="Cancel the currently running Slurm job for this rack (scancel)."
        icon={<XCircle size={20} />}
        buttonLabel="Cancel"
        destructive
        selectedNodes={selectedNodes}
      />
      <ActionCard
        title="Drain Nodes"
        description={`Drain ${nodeCount} selected node(s) — prevents new Slurm jobs from scheduling on them.`}
        icon={<ArrowDownToLine size={20} />}
        buttonLabel="Drain"
        disabled={disabled}
        selectedNodes={selectedNodes}
      />
      <ActionCard
        title="Undrain Nodes"
        description={`Resume ${nodeCount} selected node(s) — re-enables Slurm job scheduling.`}
        icon={<ArrowUpFromLine size={20} />}
        buttonLabel="Undrain"
        disabled={disabled}
        selectedNodes={selectedNodes}
      />
    </Box>
  );
}
