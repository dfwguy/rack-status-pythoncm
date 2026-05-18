import Box from "@mui/material/Box";
import { Text, Divider } from "@nvidia/foundations-react-core";
import { ActionCard } from "./ActionCard";
import { Play, Flame, Zap, Boxes, FileSearch, XCircle, ArrowDownToLine, ArrowUpFromLine, Network } from "lucide-react";
import { useRackCommands } from "../../context/RackCommandContext";
import { llamaNodesBody, slurmTargetBody, validationFamilyBody } from "../../lib/rackCommandBodies";
import type { NvisNamingSchema } from "../../types/naming";

interface ValidationTabProps {
  rack: string;
  logDir?: string;
  naming?: NvisNamingSchema;
  selectedNodes: Set<string>;
  disabled: boolean;
  jobId?: string;
  onViewResults?: () => void;
}

export function ValidationTab({ rack, logDir, naming, selectedNodes, disabled, jobId, onViewResults }: ValidationTabProps) {
  const { runAsyncValidationFamily, runSyncJson, openText } = useRackCommands();
  const nodeCount = selectedNodes.size;

  const validationBody = () => validationFamilyBody(rack, logDir, selectedNodes, naming);

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
        onRun={() =>
          runAsyncValidationFamily(
            "NEMO validation",
            "validation",
            "/api/run-validation-async",
            (jid) => `/api/run-validation-status?job_id=${encodeURIComponent(jid)}`,
            validationBody(),
          )
        }
      />
      <ActionCard
        title="Run Llama"
        description={`Launch Llama inference benchmark on ${nodeCount} selected node(s) in ${rack}. Submits a Slurm job using the Llama validation script.`}
        icon={<Flame size={20} />}
        buttonLabel="Run Llama"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() =>
          runAsyncValidationFamily(
            "Llama",
            "validation",
            "/api/run-llama-async",
            (jid) => `/api/run-llama-status?job_id=${encodeURIComponent(jid)}`,
            llamaNodesBody(rack, logDir, selectedNodes, naming),
          )
        }
      />
      <ActionCard
        title="Run Stress"
        description={`Launch MNUBERGEMM stress test on ${nodeCount} selected node(s) in ${rack}. Sustained GPU compute load for burn-in and thermal validation.`}
        icon={<Zap size={20} />}
        buttonLabel="Run Stress"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() =>
          runAsyncValidationFamily(
            "MNUBERGEMM stress",
            "stress",
            "/api/run-mnubergemm-stress-async",
            (jid) => `/api/run-mnubergemm-stress-status?job_id=${encodeURIComponent(jid)}`,
            validationBody(),
          )
        }
      />
      <ActionCard
        title="Run nvloom"
        description={`Launch nvloom (gpu-to-rack / fabric) on ${nodeCount} selected node(s) in ${rack} via Slurm. Slurm stdout: log_dir/nvloom/$USER-NVLOOM-${rack}-<jobid>.txt (NEMO-style naming).`}
        icon={<Boxes size={20} />}
        buttonLabel="Run nvloom"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() =>
          runAsyncValidationFamily(
            "nvloom",
            "validation",
            "/api/run-nvloom-async",
            (jid) => `/api/run-nvloom-status?job_id=${encodeURIComponent(jid)}`,
            validationBody(),
          )
        }
      />
      <ActionCard
        title="Run ClusterKit"
        description={`Launch HPC-X ClusterKit (IB bandwidth per HCA) on ${nodeCount} selected node(s) in ${rack}. Slurm stdout: log_dir/clusterkit/$USER-CK-${rack}-<jobid>.txt; artifacts under log_dir/clusterkit/${rack}/.`}
        icon={<Network size={20} />}
        buttonLabel="Run ClusterKit"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() =>
          runAsyncValidationFamily(
            "ClusterKit",
            "validation",
            "/api/run-clusterkit-async",
            (jid) => `/api/run-clusterkit-status?job_id=${encodeURIComponent(jid)}`,
            validationBody(),
          )
        }
      />

      <div style={{ margin: "8px 0" }}>
        <Divider />
      </div>
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
        onRun={async () => {
          const jid = (jobId ?? "").trim();
          if (!jid || jid === "-") {
            openText("Slurm scancel", "No active job id for this rack (last_job missing or placeholder).", true);
            return;
          }
          await runSyncJson("Slurm scancel", "/api/slurm-scancel", { rack, job_id: jid });
        }}
      />
      <ActionCard
        title="Drain Nodes"
        description={`Drain ${nodeCount} selected node(s) — prevents new Slurm jobs from scheduling on them.`}
        icon={<ArrowDownToLine size={20} />}
        buttonLabel="Drain"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() => runSyncJson("Slurm drain", "/api/slurm-drain", slurmTargetBody(rack, selectedNodes, naming))}
      />
      <ActionCard
        title="Undrain Nodes"
        description={`Resume ${nodeCount} selected node(s) — re-enables Slurm job scheduling.`}
        icon={<ArrowUpFromLine size={20} />}
        buttonLabel="Undrain"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() => runSyncJson("Slurm undrain", "/api/slurm-undrain", slurmTargetBody(rack, selectedNodes, naming))}
      />
    </Box>
  );
}
