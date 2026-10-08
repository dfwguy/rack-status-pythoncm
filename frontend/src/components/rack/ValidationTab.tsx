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
import { useState, lazy, Suspense } from "react";
import Box from "@mui/material/Box";
import { Banner, Text, Divider } from "@nvidia/foundations-react-core";
import { ActionCard } from "./ActionCard";
import { ActionCardGrid } from "../widgets/ActionCardGrid";
import { NvlinkPreflightPanel } from "./NvlinkPreflightPanel";
import { LaunchOptionsDialog, type NcclVersionOption } from "./LaunchOptionsDialog";
import { ValidationActivitiesTable } from "./ValidationActivitiesTable";
import { FileSearch, XCircle, ArrowDownToLine, ArrowUpFromLine } from "lucide-react";
import { useRackCommands } from "../../context/RackCommandContext";
import { useActiveCluster } from "../../context/ActiveClusterContext";
import { useBoardActions } from "../../hooks/useBoardActions";
import { ToolWidgetBoard } from "../widgets/ToolWidgetBoard";
// react-flow + dagre (~279 kB of source) ride in with the run diagram. This panel is
// already rendered only while a run is open, so `lazy()` alone finishes the job --
// see the note in App.tsx for why both halves usually matter.
const WorkflowRunPanel = lazy(() => import("../workflow/WorkflowRunPanel"));
import { startWorkflowRun } from "../../services/workflowService";
import { nodeSelectionProblem, planToolLaunch, statusUrlFor } from "../../lib/toolLaunch";
import { slurmTargetBody, validationFamilyBody, type LaunchOptions } from "../../lib/rackCommandBodies";
import { asNodeCmdKind, type NodeCmdKind } from "../../lib/rackCommandTypes";
import { ValidationPresetDialog } from "./ValidationPresetDialog";
import type { ValidationScope } from "../../lib/validationPresets";
import type { BoardAction } from "../widgets/boardActions";
import type { NvisNamingSchema } from "../../types/naming";

/** The board this tab renders. Tools are seeded onto it in `backend/catalog/seed.py`. */
const SURFACE = "rackDetail.validation";

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
  /** Which activity family the run is filed under — "validation" unless a tool says "stress". */
  family: NodeCmdKind;
  /**
   * Show the NCCL library-version selector — only for the direct NCCL path
   * (run-llmb-nccl-async → nccl_allreduce.sbatch), which resolves libnccl via
   * LD_LIBRARY_PATH. Container/NEMO launches ignore it.
   */
  showNcclVersion?: boolean;
  /**
   * Catalog-derived NCCL builds for the version selector, forwarded from the tool's launch body
   * (`nccl_versions`, injected by the backend from the P2P catalog). Undefined falls back to the
   * dialog's shipped list.
   */
  ncclVersions?: NcclVersionOption[];
  /** Show the iterations (sweep-rounds) stepper — direct NCCL paths only. */
  showIters?: boolean;
}

/** A preset launch awaiting the prepare/review gate, plus the widget that asked for it. */
interface PendingPreset {
  presetId: string;
  label: string;
  family: NodeCmdKind;
  scope: ValidationScope;
}

/** The activity family a tool's launch body asks for, narrowed to the palette's union. */
function familyOf(body: Record<string, unknown>): NodeCmdKind {
  const value = body.family;
  return asNodeCmdKind(typeof value === "string" ? value : undefined);
}

export function ValidationTab({ rack, logDir, naming, selectedNodes, disabled, jobId, onViewResults }: ValidationTabProps) {
  const { runAsyncValidationFamily, launchAsyncValidationFamily, runSyncJson, openText } = useRackCommands();
  const { activeCluster, activeProject, activePlatform } = useActiveCluster();
  const { actions, loading: toolsLoading, error: toolsError, reload: reloadActions } =
    useBoardActions();
  const nodeCount = selectedNodes.size;
  const showNvlinkPreflight = !toolsLoading && !toolsError &&
    actions.some((action) => action.kind === "tool" && action.id === "nvlink-preflight");
  const [pendingLaunch, setPendingLaunch] = useState<PendingLaunch | null>(null);
  const [pendingPreset, setPendingPreset] = useState<PendingPreset | null>(null);
  const [workflowRunId, setWorkflowRunId] = useState<string | null>(null);

  const validationBody = () => validationFamilyBody(rack, logDir, selectedNodes, naming);

  const confirmLaunch = (opts: LaunchOptions) => {
    const p = pendingLaunch;
    setPendingLaunch(null);
    if (!p) return;
    void runAsyncValidationFamily(
      p.label,
      p.family,
      p.postUrl,
      p.statusUrl,
      validationFamilyBody(rack, logDir, selectedNodes, naming, opts),
    );
  };

  /**
   * Turn a widget press into a launch, using the machinery this tab already owns.
   *
   * The widget itself never POSTs. Everything still goes through the options dialog, the
   * preset prepare/review gate and the command store, so a catalog-driven button and a
   * hand-written one are the same launch with the same audit trail — the document only
   * decides which of them runs.
   */
  const runTool = (action: BoardAction) => {
    // A workflow is not a catalog tool: the engine owns its run surface, and the board's rack
    // context is passed as parameters so a flow authored generically lands on the right rack.
    if (action.kind === "workflow") {
      void startWorkflowRun(action.id, {
        rack,
        target: [...selectedNodes].sort().join(","),
      })
        .then((res) => setWorkflowRunId(res.run_id))
        .catch((err: unknown) =>
          openText(
            action.name,
            `Could not start the workflow: ${err instanceof Error ? err.message : String(err)}`,
            true,
          ),
        );
      return;
    }
    const tool = action.tool;
    if (!tool) return;
    const plan = planToolLaunch(tool);
    switch (plan.kind) {
      case "validation-preset": {
        // A recipe that accepts single nodes launches per-node when exactly one is picked;
        // otherwise the whole rack. The scope list is the catalog's, not a hard-coded id check.
        const perNode = plan.scopes.includes("node") && nodeCount === 1;
        setPendingPreset({
          presetId: plan.presetId,
          label: tool.name,
          family: familyOf(tool.launch.body ?? {}),
          scope: perNode ? "nodes" : "rack",
        });
        return;
      }
      case "async-job": {
        const family = familyOf(plan.body);
        if (plan.dialog === "launch-options") {
          setPendingLaunch({
            label: tool.name,
            postUrl: plan.endpoint,
            statusUrl: (jid) => statusUrlFor(plan.endpoint, jid),
            title: `Launch ${tool.name}`,
            family,
            showNcclVersion: plan.body.show_nccl_version === true,
            ncclVersions: Array.isArray(plan.body.nccl_versions)
              ? (plan.body.nccl_versions as NcclVersionOption[])
              : undefined,
            showIters: plan.body.show_iters === true,
          });
          return;
        }
        void runAsyncValidationFamily(
          tool.name,
          family,
          plan.endpoint,
          (jid) => statusUrlFor(plan.endpoint, jid),
          validationBody(),
        );
        return;
      }
      default:
        // `stream`, `dialog` and `unsupported` have no handler on this surface yet. Say so
        // rather than no-op: a button that does nothing when pressed is indistinguishable
        // from a stalled cluster.
        openText(
          tool.name,
          `This action cannot be launched from the Validation tab: ${
            plan.kind === "unsupported" ? plan.reason : `unsupported launch kind "${plan.kind}"`
          }.`,
          true,
        );
    }
  };

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      {/* First, above the stages: the cheap gate that decides whether the expensive ones are
          worth starting. A wedged NVSwitch route makes NCCL/NeMo hang rather than fail. */}
      {showNvlinkPreflight ? (
        <>
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
        </>
      ) : null}

      <Text kind="body/bold/sm" style={{ color: "var(--text-color-secondary)" }}>
        Validation Actions
      </Text>
      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginTop: -8 }}>
        These come from the Tool catalog for this cluster, so a new script or a version bump
        appears here without a release. Cross-rack work — Stage 3 and the Llama 3.1 405B FP8
        256-node / 1024-GPU profile — runs from Multirack Validation.
      </Text>

      {toolsError ? (
        <Banner status="warning" kind="inline">
          Could not load the tool catalog ({toolsError}). Validation actions are unavailable
          until it loads.
        </Banner>
      ) : null}

      {toolsLoading ? (
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-subtle)" }}>
          Loading actions…
        </Text>
      ) : (
        <ToolWidgetBoard
          surface={SURFACE}
          actions={actions}
          clusterId={activeCluster?.id}
          disabled={disabled}
          blockedReason={(action) => {
            const selectionProblem = action.tool
              ? nodeSelectionProblem(action.tool, nodeCount)
              : null;
            if (selectionProblem || rack || action.kind === "workflow") return selectionProblem;
            const plan = action.tool ? planToolLaunch(action.tool) : null;
            if (plan?.kind === "validation-preset" && plan.scopes.includes("node")) return null;
            return "This action requires a physical rack target.";
          }}
          onRun={runTool}
          // Stamped into an exported bundle and compared on import, so a board shared between
          // deployments says where it came from and warns on a platform mismatch.
          bundleSource={{
            product_version: "",
            cluster: activeCluster?.id ?? "",
            platform: activePlatform?.id ?? "",
            customer: activeProject?.customer ?? "",
          }}
          onCatalogChanged={reloadActions}
        />
      )}

      {logDir?.trim() && rack ? (
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
        Results &amp; Controls
      </Text>

      <ActionCardGrid>
        <ActionCard
          title="View Parser Results"
          category="validation"
          description="Parse the latest validation log and show NCCL bandwidth summary, step-time charts, and key metrics."
          icon={<FileSearch size={20} />}
          buttonLabel="View"
          disabled={!onViewResults}
          onRun={onViewResults}
        />
        <ActionCard
          title="Cancel Running Job"
          category="maintenance"
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
            await runSyncJson("Slurm scancel", "/api/scheduler-cancel", { rack, job_id: jid });
          }}
        />
        <ActionCard
          title="Drain Nodes"
          category="maintenance"
          description={`Drain ${nodeCount} selected node(s) — prevents new Slurm jobs from scheduling on them.`}
          icon={<ArrowDownToLine size={20} />}
          buttonLabel="Drain"
          disabled={disabled}
          selectedNodes={selectedNodes}
          onRun={() => runSyncJson("Slurm drain", "/api/scheduler-drain", slurmTargetBody(rack, selectedNodes, naming))}
        />
        <ActionCard
          title="Undrain Nodes"
          category="maintenance"
          description={`Resume ${nodeCount} selected node(s) — re-enables Slurm job scheduling.`}
          icon={<ArrowUpFromLine size={20} />}
          buttonLabel="Undrain"
          disabled={disabled}
          selectedNodes={selectedNodes}
          onRun={() => runSyncJson("Slurm undrain", "/api/scheduler-resume", slurmTargetBody(rack, selectedNodes, naming))}
        />
      </ActionCardGrid>

      <LaunchOptionsDialog
        open={pendingLaunch !== null}
        onClose={() => setPendingLaunch(null)}
        onConfirm={confirmLaunch}
        title={pendingLaunch?.title ?? "Launch options"}
        subtitle={`${nodeCount} selected node(s)${rack ? ` in ${rack}` : ""}`}
        confirmLabel="Launch"
        showNcclVersion={pendingLaunch?.showNcclVersion ?? false}
        ncclVersions={pendingLaunch?.ncclVersions}
        showIters={pendingLaunch?.showIters ?? false}
      />

      {workflowRunId ? (
        <Suspense fallback={null}>
          <WorkflowRunPanel runId={workflowRunId} onClose={() => setWorkflowRunId(null)} />
        </Suspense>
      ) : null}

      {pendingPreset ? (
        <ValidationPresetDialog
          open
          onClose={() => setPendingPreset(null)}
          presetId={pendingPreset.presetId}
          fallbackLabel={pendingPreset.label}
          scope={pendingPreset.scope}
          nodes={selectedNodes}
          racks={rack ? [rack] : []}
          logDir={logDir}
          onLaunch={(request) =>
            launchAsyncValidationFamily(
              pendingPreset.label,
              pendingPreset.family,
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
