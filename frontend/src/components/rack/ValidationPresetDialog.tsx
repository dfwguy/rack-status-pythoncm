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
import { useEffect, useMemo, useState } from "react";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import MuiButton from "@mui/material/Button";
import {
  Banner,
  Button,
  Checkbox,
  CodeSnippet,
  Divider,
  Flex,
  SegmentedControl,
  Tag,
  Text,
} from "@nvidia/foundations-react-core";
import { ClipboardCheck, Database, ShieldCheck } from "lucide-react";
import {
  VALIDATION_PRESET_LABELS,
  buildValidationPresetRequest,
  formatPresetRequirements,
  presetLocalContainerDir,
  presetNodeImagesP2pManaged,
  type ValidationPreset,
  type ValidationPresetPlan,
  type ValidationPresetPrepareResponse,
  type ValidationPresetRequest,
  type ValidationPresetsResponse,
  type ValidationScope,
  type ValidationStorage,
} from "../../lib/validationPresets";
import { apiFetch } from "../../lib/apiFetch";

const STORAGE_ITEMS = [
  { value: "nfs", children: "Shared NFS" },
  { value: "local", children: "Node-local" },
];

interface ValidationPresetDialogProps {
  open: boolean;
  onClose: () => void;
  /**
   * Recipe id. A plain string, not the `ValidationPresetId` union: a widget's id comes from
   * the catalog at runtime, so a union compiled into this bundle cannot be the gate. The
   * server refuses an unknown id in `resolve_preset`.
   */
  presetId: string;
  /** Title while the server catalogue loads; the fetched label wins once it arrives. */
  fallbackLabel?: string;
  scope: ValidationScope;
  nodes?: Iterable<string>;
  racks?: Iterable<string>;
  target?: string;
  logDir?: string;
  campaignId?: string;
  campaignVersion?: string;
  onLaunch: (request: ValidationPresetRequest, preset: ValidationPreset) => void | Promise<unknown>;
}

/**
 * Shared launch gate for validated recipes. It deliberately separates prepare from launch:
 * the operator sees the resolved target, versions, checks, staging, and exact command before
 * the async endpoint can be called. Any option change invalidates the reviewed plan.
 */
export function ValidationPresetDialog({
  open,
  onClose,
  presetId,
  fallbackLabel,
  scope,
  nodes,
  racks,
  target,
  logDir,
  campaignId,
  campaignVersion,
  onLaunch,
}: ValidationPresetDialogProps) {
  const nodeList = useMemo(() => (nodes ? [...nodes] : []), [nodes]);
  const rackList = useMemo(() => (racks ? [...racks] : []), [racks]);
  const [presets, setPresets] = useState<ValidationPreset[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState("");
  const [storage, setStorage] = useState<ValidationStorage>("nfs");
  const [rsyncToLocal, setRsyncToLocal] = useState(false);
  const [plan, setPlan] = useState<ValidationPresetPlan | null>(null);
  const [prepareError, setPrepareError] = useState("");
  const [preparing, setPreparing] = useState(false);
  const [launching, setLaunching] = useState(false);

  // Typed as a full ValidationPreset so the placeholder and the fetched record are the same
  // shape: with catalog-supplied ids the placeholder is now reachable for recipes this bundle
  // has never heard of, and a narrower fallback would make every optional field a union.
  const preset: ValidationPreset = presets.find((item) => item.id === presetId) ?? {
    id: presetId,
    label: VALIDATION_PRESET_LABELS[presetId] ?? fallbackLabel ?? presetId,
    description: "Validated cluster recipe. Server defaults and requirements load from the active cluster.",
    kind: presetId,
  };
  // The active cluster's node image cache, as the server reports it. Until the catalog loads,
  // name it generically rather than guess a path that may not exist on this cluster.
  const localDir = presetLocalContainerDir(preset) ?? "the cluster's node image cache";
  const p2pManaged = presetNodeImagesP2pManaged(preset);

  const request = useMemo(
    () =>
      buildValidationPresetRequest({
        presetId,
        scope,
        nodes: nodeList,
        racks: rackList,
        target,
        logDir,
        storage,
        rsyncToLocal,
        campaign: campaignId,
        campaignVersion,
      }),
    [presetId, scope, nodeList, rackList, target, logDir, storage, rsyncToLocal, campaignId, campaignVersion],
  );

  useEffect(() => {
    if (!open) return;
    setStorage("nfs");
    setRsyncToLocal(false);
    setPlan(null);
    setPrepareError("");
    setCatalogError("");
    setCatalogLoading(true);
    let cancelled = false;
    void apiFetch("/api/validation-presets")
      .then(async (response) => {
        const body = (await response.json()) as ValidationPresetsResponse;
        if (!response.ok || !body.ok || !Array.isArray(body.presets)) {
          throw new Error(body.error || `Preset catalog failed (HTTP ${response.status})`);
        }
        if (!cancelled) {
          setPresets(body.presets);
          const loadedPreset = body.presets.find((item) => item.id === presetId);
          const defaultStorage = loadedPreset?.defaults?.storage;
          setStorage(defaultStorage === "local" ? "local" : "nfs");
          setRsyncToLocal(loadedPreset?.defaults?.rsync === true);
          setPlan(null);
          setCatalogLoading(false);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setCatalogError(error instanceof Error ? error.message : "Preset catalog is unavailable");
          setCatalogLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [open, presetId]);

  const invalidatePlan = () => {
    setPlan(null);
    setPrepareError("");
  };

  const prepare = async () => {
    setPreparing(true);
    setPrepareError("");
    setPlan(null);
    try {
      const response = await apiFetch("/api/validation-presets/prepare", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
      });
      const body = (await response.json()) as ValidationPresetPrepareResponse;
      if (!response.ok || !body.ok || !body.plan) {
        throw new Error(body.error || `Plan failed (HTTP ${response.status})`);
      }
      setPlan(body.plan);
    } catch (error) {
      setPrepareError(error instanceof Error ? error.message : "Could not prepare validation plan");
    } finally {
      setPreparing(false);
    }
  };

  const launch = async () => {
    if (!plan) return;
    setLaunching(true);
    try {
      await onLaunch({ ...request, plan_hash: plan.plan_hash }, preset);
      onClose();
    } catch (error) {
      setPrepareError(error instanceof Error ? error.message : "Launch failed");
    } finally {
      setLaunching(false);
    }
  };

  const requirements = formatPresetRequirements(preset.requirements);
  const targetCount = nodeList.length || rackList.length;
  const targetNoun = nodeList.length > 0 ? "node" : "rack";
  const directHostPreset = presetId === "nccl-2.31.2";
  // A recipe that runs no container has nothing to stage; the server's catalog says so with a null
  // container_profile. Before the catalog loads, only the known direct-host recipe is assumed.
  const noContainer =
    directHostPreset ||
    (preset.defaults !== undefined && "container_profile" in preset.defaults && !preset.defaults.container_profile);
  const supportedScopes =
    preset.requirements && !Array.isArray(preset.requirements) && Array.isArray(preset.requirements.scopes)
      ? preset.requirements.scopes.map(String)
      : [];
  const serverScope = scope === "nodes" ? "node" : scope;
  const unsupportedScope = supportedScopes.length > 0 && !supportedScopes.includes(serverScope);

  return (
    <Dialog open={open} onClose={launching ? undefined : onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontWeight: 600 }}>{preset.label}</DialogTitle>
      <DialogContent dividers>
        <Flex direction="column" gap="4">
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            {preset.description}
          </Text>

          {presetId === "llama31-405b-fp8" ? (
            <Banner status="info">
              Llama 3.1 405B FP8 is invariantly pinned to exactly 256 nodes / 1024 GB300 GPUs, TP4 PP8 CP1 VP4, 50 steps, NeMo 26.04.01 + NCCL 2.30.7, and NVLS disabled. The server rejects scale or step overrides.
            </Banner>
          ) : null}
          {presetId === "exemplar-system-info" ? (
            <Banner status="info">
              NVIDIA DGXC v26.06.01 system_info fans out to every selected node and validates each result against the Exemplar recipe.
            </Banner>
          ) : null}
          {presetId === "gpt-oss-cpu" && nodeList.length > 1 ? (
            <Banner status="info">
              GPT-OSS stays a single-node recipe. Greenlight will submit one independent job for each of the {nodeList.length} exact selected nodes.
            </Banner>
          ) : null}
          {presetId === "mnubergemm-26.03-v1.17" && rackList.length > 1 ? (
            <Banner status="info">
              Greenlight submits one independent, rack-scoped MNUberGEMM job per selected rack. It never combines physical racks into one MNNVL domain.
            </Banner>
          ) : null}
          {presetId === "node-gate" ? (
            <Banner status="warning">
              The gate holds the selected nodes exclusively for up to 20 minutes and kills any process still on
              their GPUs before it checks them. Run it on idle nodes between jobs; a node that fails stays as it
              is for you to repair or drain.
            </Banner>
          ) : null}
          {unsupportedScope ? (
            <Banner status="error">
              This recipe supports {supportedScopes.join(", ")} scope, not {serverScope}. Choose it from a supported target view.
            </Banner>
          ) : null}
          {directHostPreset ? (
            <Banner status="info">NCCL 2.31.2 is a fixed direct-host CUDA 13.3+ validation. It does not use or stage a container.</Banner>
          ) : null}
          {catalogError ? <Banner status="warning">Preset catalog: {catalogError}. Prepare remains server-validated.</Banner> : null}

          <Flex gap="2" wrap="wrap" align="center">
            <Tag color="blue" kind="outline" readOnly density="compact">
              {scope}
            </Tag>
            <Tag color="gray" kind="outline" readOnly density="compact">
              {targetCount} selected {targetNoun}{targetCount === 1 ? "" : "s"}
            </Tag>
            {presetId === "nccl-2.31.2" ? (
              <Tag color="green" kind="solid" readOnly density="compact">CUDA 13.3+ · NCCL 2.31.2</Tag>
            ) : null}
            {presetId === "nemo-26.04.01" ? (
              <Tag color="purple" kind="outline" readOnly density="compact">Container 26.04.01</Tag>
            ) : null}
            {presetId === "mnubergemm-26.03-v1.17" ? (
              <Tag color="purple" kind="outline" readOnly density="compact">Container 26.03-v1.17</Tag>
            ) : null}
          </Flex>

          {requirements.length > 0 ? (
            <div>
              <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 6 }}>Recipe requirements</Text>
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                {requirements.join(" · ")}
              </Text>
            </div>
          ) : null}

          {!noContainer ? (
            <>
              <Divider />
              <div>
                <Flex gap="2" align="center" style={{ marginBottom: 8 }}>
                  <Database size={18} />
                  <Text kind="body/bold/sm">Container storage</Text>
                </Flex>
                <SegmentedControl
                  size="small"
                  items={STORAGE_ITEMS}
                  value={storage}
                  onValueChange={(value) => {
                    const nextStorage = value === "local" ? "local" : "nfs";
                    setStorage(nextStorage);
                    if (nextStorage === "nfs") setRsyncToLocal(false);
                    invalidatePlan();
                  }}
                  aria-label="Container storage mode"
                />
                <Text kind="body/regular/sm" style={{ display: "block", color: "var(--text-color-secondary)", marginTop: 6 }}>
                  Shared NFS runs the image from the configured store. Node-local uses {localDir} on every selected HGX/DGX node.
                </Text>
              </div>

              {p2pManaged ? (
                <Text kind="body/regular/sm" style={{ display: "block", color: "var(--text-color-secondary)" }}>
                  NVIS-P2P distributes images on this cluster, so node-local runs verify the image in {localDir} and
                  never copy it from the head. Run a P2P campaign first if a node does not have it.
                </Text>
              ) : (
              <Checkbox
                checked={rsyncToLocal}
                onCheckedChange={(checked) => {
                  setRsyncToLocal(checked === true);
                  if (checked === true) setStorage("local");
                  invalidatePlan();
                }}
                slotLabel={
                  <div>
                    <Text kind="body/bold/sm" style={{ display: "block" }}>Rsync container before validation</Text>
                    <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                      Check the image locally, then rsync only missing/stale content into {localDir} to avoid cold starts.
                    </Text>
                  </div>
                }
              />
              )}
            </>
          ) : null}

          {prepareError ? <Banner status="error">{prepareError}</Banner> : null}

          {plan ? (
            <>
              <Divider />
              <Flex gap="2" align="center">
                <ClipboardCheck size={18} />
                <Text kind="title/sm">Dry-run plan</Text>
                <Tag color="green" kind="solid" readOnly density="compact">ready to launch</Tag>
              </Flex>
              <Text kind="body/regular/sm">{plan.summary}</Text>
              <Flex gap="2" wrap="wrap">
                {plan.node_count !== undefined ? (
                  <Tag color="blue" kind="outline" readOnly density="compact">{plan.node_count} resolved nodes</Tag>
                ) : null}
                {plan.cuda_version ? (
                  <Tag color="gray" kind="outline" readOnly density="compact">CUDA {plan.cuda_version}</Tag>
                ) : null}
                {plan.nccl_version ? (
                  <Tag color="gray" kind="outline" readOnly density="compact">NCCL {plan.nccl_version}</Tag>
                ) : null}
                {plan.max_steps !== undefined ? (
                  <Tag color="gray" kind="outline" readOnly density="compact">{plan.max_steps} steps</Tag>
                ) : null}
                {plan.requires_infiniband ? (
                  <Tag color="teal" kind="outline" readOnly density="compact">/dev/infiniband required</Tag>
                ) : null}
              </Flex>
              {plan.checks?.length ? (
                <div>
                  <Flex gap="2" align="center" style={{ marginBottom: 6 }}>
                    <ShieldCheck size={18} />
                    <Text kind="body/bold/sm">Preflight checks</Text>
                  </Flex>
                  {plan.checks.map((check) => (
                    <Text key={check} kind="body/regular/sm" style={{ display: "block", color: "var(--text-color-secondary)" }}>
                      • {check}
                    </Text>
                  ))}
                </div>
              ) : null}
              {plan.command_display ? (
                <div>
                  <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 6 }}>Command preview</Text>
                  <CodeSnippet language="bash" value={plan.command_display} />
                </div>
              ) : null}
            </>
          ) : null}
        </Flex>
      </DialogContent>
      <DialogActions sx={{ px: 3, py: 2 }}>
        <MuiButton onClick={onClose} color="inherit" disabled={launching}>Cancel</MuiButton>
        <Button kind="secondary" size="small" onClick={() => void prepare()} disabled={unsupportedScope || catalogLoading || preparing || launching}>
          {catalogLoading ? "Loading recipe…" : preparing ? "Preparing…" : plan ? "Refresh dry run" : "Review dry run"}
        </Button>
        <Button kind="primary" color="brand" size="small" onClick={() => void launch()} disabled={!plan || launching}>
          {launching ? "Launching…" : "Launch validation"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
