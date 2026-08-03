import { useState } from "react";
import Box from "@mui/material/Box";
import { Text, Divider } from "@nvidia/foundations-react-core";
import { ActionCard } from "./ActionCard";
import { LaunchOptionsDialog } from "./LaunchOptionsDialog";
import { ValidationActivitiesTable } from "./ValidationActivitiesTable";
import { Play, Flame, Zap, Boxes, FileSearch, XCircle, ArrowDownToLine, ArrowUpFromLine, Network, Gauge, Activity } from "lucide-react";
import { useRackCommands } from "../../context/RackCommandContext";
import { llamaNodesBody, slurmTargetBody, validationFamilyBody, type LaunchOptions } from "../../lib/rackCommandBodies";
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

/** A NCCL/NEMO launch deferred until the user confirms options in the modal. */
interface PendingLaunch {
  label: string;
  postUrl: string;
  statusUrl: (jobId: string) => string;
  /** Modal title/subtitle for context. */
  title: string;
  /**
   * Show the NCCL library-version selector — only for the direct NCCL path
   * (run-llmb-nccl-async → nccl_allreduce.sbatch), which resolves libnccl via
   * LD_LIBRARY_PATH. Container/NEMO launches ignore it.
   */
  showNcclVersion?: boolean;
  /** Show the iterations (sweep-rounds) stepper — direct NCCL paths only. */
  showIters?: boolean;
}

export function ValidationTab({ rack, logDir, naming, selectedNodes, disabled, jobId, onViewResults }: ValidationTabProps) {
  const { runAsyncValidationFamily, runSyncJson, openText } = useRackCommands();
  const nodeCount = selectedNodes.size;
  const [pendingLaunch, setPendingLaunch] = useState<PendingLaunch | null>(null);

  const validationBody = () => validationFamilyBody(rack, logDir, selectedNodes, naming);

  const confirmLaunch = (opts: LaunchOptions) => {
    const p = pendingLaunch;
    setPendingLaunch(null);
    if (!p) return;
    void runAsyncValidationFamily(
      p.label,
      "validation",
      p.postUrl,
      p.statusUrl,
      validationFamilyBody(rack, logDir, selectedNodes, naming, opts),
    );
  };

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <Text kind="body/bold/sm" style={{ color: "var(--text-color-secondary)", marginTop: 8 }}>
        Validation Stages
      </Text>

      <ActionCard
        title="Stage 1 — NCCL Benchmark"
        description={`Stage-1 NCCL acceptance on ${nodeCount} selected node(s) in ${rack}, submitted via llmb-run (workload microbenchmark_nccl): the full 24-test sweep across all_reduce / all_gather / reduce_scatter / alltoall / sendrecv up to 16G, including the NVLS variants. Writes an NCCL-1Rack log to log_dir so it appears on the dashboard + parser results.`}
        icon={<Activity size={20} />}
        buttonLabel="Run NCCL"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() =>
          setPendingLaunch({
            label: "NCCL Benchmark (Stage-1)",
            postUrl: "/api/run-nccl-fullrack-async",
            statusUrl: (jid) => `/api/run-nccl-fullrack-status?job_id=${encodeURIComponent(jid)}`,
            title: "Launch Stage-1 NCCL Benchmark",
            showNcclVersion: true,
            showIters: true,
          })
        }
      />
      <ActionCard
        title="Stage 2 — NEMO Training (NCCL + LLMB)"
        description={`Stage-2 single-rack validation on ${nodeCount} selected node(s) in ${rack}, both halves submitted via llmb-run: the NCCL sweep (microbenchmark_nccl) followed by Nemotron-H 56B fp8 pretraining (pretrain_nemotron-h). Slurm serialises the two jobs on the rack.`}
        icon={<Play size={20} />}
        buttonLabel="Run NEMO"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() =>
          setPendingLaunch({
            label: "NEMO validation",
            postUrl: "/api/run-validation-async",
            statusUrl: (jid) => `/api/run-validation-status?job_id=${encodeURIComponent(jid)}`,
            title: "Launch Stage-2 NEMO Training",
          })
        }
      />

      {logDir?.trim() ? (
        <>
          <div style={{ margin: "8px 0" }}>
            <Divider />
          </div>
          <ValidationActivitiesTable logDir={logDir} rack={rack} title="Validation activities" />
        </>
      ) : null}

      <div style={{ margin: "8px 0" }}>
        <Divider />
      </div>
      <Text kind="body/bold/sm" style={{ color: "var(--text-color-secondary)" }}>
        Optional Tests &amp; Diagnostics
      </Text>
      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginTop: -8 }}>
        Stage 3 (cross-rack) runs from the multi-rack action menu.
      </Text>

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

      <ActionCard
        title="Run NCCL Microbenchmark (LLMB)"
        description={`Launch the NCCL microbenchmark (llmb-run microbenchmark_nccl, fp8) on ${rack}. Resolves NVIS rack ids to real Slurm nodes and computes GPU scale (nodes*4); single rack = scale 72 on GB300 NVL72.`}
        icon={<Gauge size={20} />}
        buttonLabel="Run LLMB NCCL"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() =>
          setPendingLaunch({
            label: "NCCL Microbenchmark (LLMB)",
            postUrl: "/api/run-llmb-nccl-async",
            statusUrl: (jid) => `/api/run-llmb-nccl-status?job_id=${encodeURIComponent(jid)}`,
            title: "Launch NCCL Microbenchmark (LLMB)",
            showNcclVersion: true,
            showIters: true,
          })
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

      <LaunchOptionsDialog
        open={pendingLaunch !== null}
        onClose={() => setPendingLaunch(null)}
        onConfirm={confirmLaunch}
        title={pendingLaunch?.title ?? "Launch options"}
        subtitle={`${nodeCount} selected node(s) in ${rack}`}
        confirmLabel="Launch"
        showNcclVersion={pendingLaunch?.showNcclVersion ?? false}
        showIters={pendingLaunch?.showIters ?? false}
      />
    </Box>
  );
}
