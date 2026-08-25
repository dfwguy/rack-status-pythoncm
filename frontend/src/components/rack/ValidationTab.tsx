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
import { useState } from "react";
import Box from "@mui/material/Box";
import { Text, Divider } from "@nvidia/foundations-react-core";
import { ActionCard } from "./ActionCard";
import { NvlinkPreflightPanel } from "./NvlinkPreflightPanel";
import { LaunchOptionsDialog } from "./LaunchOptionsDialog";
import { ValidationActivitiesTable } from "./ValidationActivitiesTable";
import { Play, Flame, Zap, Boxes, FileSearch, XCircle, ArrowDownToLine, ArrowUpFromLine, Network, Gauge, Activity, Cpu, Database } from "lucide-react";
import { useRackCommands } from "../../context/RackCommandContext";
import { slurmTargetBody, validationFamilyBody, type LaunchOptions } from "../../lib/rackCommandBodies";
import { ValidationPresetDialog } from "./ValidationPresetDialog";
import { VALIDATION_PRESET_LABELS, type ValidationPresetId, type ValidationPresetRequest } from "../../lib/validationPresets";
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
  const { runAsyncValidationFamily, launchAsyncValidationFamily, runSyncJson, openText } = useRackCommands();
  const nodeCount = selectedNodes.size;
  const [pendingLaunch, setPendingLaunch] = useState<PendingLaunch | null>(null);
  const [presetDialog, setPresetDialog] = useState<ValidationPresetId | null>(null);

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
      {/* First, above the stages: the cheap gate that decides whether the expensive ones are
          worth starting. A wedged NVSwitch route makes NCCL/NeMo hang rather than fail. */}
      <Text kind="body/bold/sm" style={{ color: "var(--text-color-secondary)", marginTop: 8 }}>
        Preflight
      </Text>

      <NvlinkPreflightPanel
        rack={rack}
        naming={naming}
        selectedNodes={selectedNodes}
        disabled={disabled}
      />

      <Divider />

      <Text kind="body/bold/sm" style={{ color: "var(--text-color-secondary)" }}>
        Validation Stages
      </Text>
      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
        New validation submissions are blocked: no installed recipe is yet certified as an exact,
        ProgramRunner-only equivalent on Nscale. Existing results remain available below.
      </Text>

      <ActionCard
        title="NCCL 2.31.2 Validation"
        description="Blocked: the NCCL recipe still depends on MPI-over-SSH and is not ProgramRunner-only."
        icon={<Gauge size={20} />}
        buttonLabel="Plan NCCL"
        disabled
        onRun={() => setPresetDialog("nccl-2.31.2")}
      />

      <ActionCard
        title="Stage 1 — NCCL Benchmark"
        description="Blocked: the installed multi-node NCCL launcher is not ProgramRunner-only."
        icon={<Activity size={20} />}
        buttonLabel="Run NCCL"
        disabled
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
        description="Blocked: no validated ProgramRunner-only NeMo/Nemotron launcher exists on Nscale."
        icon={<Play size={20} />}
        buttonLabel="Run NEMO"
        disabled
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
        title="Llama 3.1 405B FP8 — 1024 GPUs / 50 Steps"
        description="Blocked: no validated ProgramRunner-only Llama training launcher exists on Nscale."
        icon={<Flame size={20} />}
        buttonLabel="Multirack Only"
        disabled
      />
      <ActionCard
        title="GPT-OSS CPU Overhead Microbenchmark — Single Node"
        description="Blocked: the GPT-OSS recipe has no validated ProgramRunner-only form."
        icon={<Cpu size={20} />}
        buttonLabel="Plan GPT-OSS"
        disabled
        onRun={() => setPresetDialog("gpt-oss-cpu")}
      />
      <ActionCard
        title="NeMo 26.04.01 Validation"
        description="Blocked: the NeMo recipe has no validated ProgramRunner-only form."
        icon={<Database size={20} />}
        buttonLabel="Plan NeMo"
        disabled
        onRun={() => setPresetDialog("nemo-26.04.01")}
      />
      <ActionCard
        title="MNUberGEMM 26.03-v1.17 Validation"
        description="Blocked: the proposed direct command does not preserve the pinned-container MNUberGEMM semantics."
        icon={<Zap size={20} />}
        buttonLabel="Plan MNUberGEMM"
        disabled
        onRun={() => setPresetDialog("mnubergemm-26.03-v1.17")}
      />
      <ActionCard
        title="Run Stress"
        description="Blocked until the pinned MNUberGEMM workload has an equivalent ProgramRunner-only implementation."
        icon={<Zap size={20} />}
        buttonLabel="Run Stress"
        disabled
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
        description="Blocked: the installed NVLoom recipe contains hidden SSH fan-out."
        icon={<Boxes size={20} />}
        buttonLabel="Run nvloom"
        disabled
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
        description="Blocked: the external ClusterKit multi-host launcher is not certified ProgramRunner-only."
        icon={<Network size={20} />}
        buttonLabel="Run ClusterKit"
        disabled
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
        description="Blocked: the LLMB NCCL replacement still launches MPI through SSH."
        icon={<Gauge size={20} />}
        buttonLabel="Run LLMB NCCL"
        disabled
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
        description="Cancel the currently running scheduler task for this rack."
        icon={<XCircle size={20} />}
        buttonLabel="Cancel"
        destructive
        selectedNodes={selectedNodes}
        onRun={async () => {
          const jid = (jobId ?? "").trim();
          if (!jid || jid === "-") {
            openText("Cancel job", "No active job id for this rack (last_job missing or placeholder).", true);
            return;
          }
          await runSyncJson("Cancel job", "/api/scheduler-cancel", { rack, job_id: jid });
        }}
      />
      <ActionCard
        title="Drain Nodes"
        description={`Drain ${nodeCount} selected node(s) — prevents new work from starting on them.`}
        icon={<ArrowDownToLine size={20} />}
        buttonLabel="Drain"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() => runSyncJson("Drain nodes", "/api/scheduler-drain", slurmTargetBody(rack, selectedNodes, naming))}
      />
      <ActionCard
        title="Undrain Nodes"
        description={`Resume ${nodeCount} selected node(s) — makes them available for direct execution.`}
        icon={<ArrowUpFromLine size={20} />}
        buttonLabel="Undrain"
        disabled={disabled}
        selectedNodes={selectedNodes}
        onRun={() => runSyncJson("Resume nodes", "/api/scheduler-resume", slurmTargetBody(rack, selectedNodes, naming))}
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

      {presetDialog ? (
        <ValidationPresetDialog
          open
          onClose={() => setPresetDialog(null)}
          presetId={presetDialog}
          scope={presetDialog === "nemo-26.04.01" ? "rack" : nodeCount === 1 ? "nodes" : "rack"}
          nodes={selectedNodes}
          racks={[rack]}
          logDir={logDir}
          onLaunch={(request: ValidationPresetRequest) =>
            launchAsyncValidationFamily(
              VALIDATION_PRESET_LABELS[presetDialog],
              presetDialog === "mnubergemm-26.03-v1.17" ? "stress" : "validation",
              "/api/run-validation-preset-async",
              (jid) => `/api/run-validation-preset-status?job_id=${encodeURIComponent(jid)}`,
              request,
            )
          }
        />
      ) : null}
    </Box>
  );
}
