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
/**
 * Validated recipe ids exposed by GET /api/validation-presets.
 *
 * Advisory since the Tool catalog started carrying recipe ids: a widget's
 * `launch.preset_id` arrives as a plain string from the server, and a union compiled into
 * last week's bundle cannot be the gate on what today's catalog offers. The real check is
 * `validation_presets.resolve_preset`, which is server-side and refuses an unknown id. Keep
 * this for the hand-written call sites that still name a recipe in code.
 */
export type ValidationPresetId =
  | "nccl-2.31.2"
  | "llama31-405b-fp8"
  | "gpt-oss-cpu"
  | "exemplar-system-info"
  | "nemo-26.04.01"
  | "mnubergemm-26.03-v1.17"
  | "node-gate";

export type ValidationScope = "nodes" | "rack" | "multirack";
export type ValidationStorage = "nfs" | "local";

export interface ValidationPreset {
  /** Server-supplied recipe id; a plain string since the catalog carries these (see above). */
  id: string;
  label: string;
  description: string;
  kind: string;
  defaults?: Record<string, unknown>;
  requirements?: Record<string, unknown> | string[];
}

export interface ValidationPresetsResponse {
  ok: boolean;
  presets?: ValidationPreset[];
  error?: string;
}

export interface ValidationPresetRequest extends Record<string, unknown> {
  preset_id: string;
  scope: ValidationScope;
  nodes?: string[];
  racks?: string[];
  target?: string;
  log_dir?: string;
  storage: ValidationStorage;
  rsync_to_local: boolean;
  campaign?: string;
  campaign_version?: string;
  /** Opaque digest returned by prepare; launch uses it to reject reviewed-target drift. */
  plan_hash?: string;
}

export interface ValidationPresetPlan {
  preset_id: string;
  validation?: string;
  summary: string;
  scope: ValidationScope | "node";
  nodes?: string[];
  racks?: string[];
  target?: string;
  node_count?: number;
  storage?: ValidationStorage;
  rsync_to_local?: boolean;
  local_container_dir?: string;
  container_profile?: string;
  cuda_version?: string;
  nccl_version?: string;
  max_steps?: number;
  requires_infiniband?: boolean;
  checks?: string[];
  command_display?: string;
  plan_hash: string;
}

export interface ValidationPresetPrepareResponse {
  ok: boolean;
  plan?: ValidationPresetPlan;
  error?: string;
}

/** Labels remain available while the server catalog is loading or unavailable. */
export const VALIDATION_PRESET_LABELS: Record<string, string> = {
  "nccl-2.31.2": "NCCL 2.31.2 / CUDA 13.3+",
  "llama31-405b-fp8": "Llama 3.1 405B FP8 — 1024 GPUs / 50 steps",
  "gpt-oss-cpu": "GPT-OSS CPU overhead microbenchmark — one independent job per selected node",
  "exemplar-system-info": "NVIDIA DGXC v26.06.01 system_info",
  "nemo-26.04.01": "NeMo 26.04.01",
  "mnubergemm-26.03-v1.17": "MNUberGEMM 26.03-v1.17",
  "node-gate": "Node gate — cleanup + health check between jobs",
};

/**
 * The node-local image cache a preset stages into. It is the active cluster's
 * `connection.node_container_dir`, which the server reports in each preset's defaults, so the UI
 * never sends one: the server owns it and refuses a request that names a different directory.
 */
/** NVIS-P2P fills this cluster's node cache; launches verify the image and never rsync it. */
export function presetNodeImagesP2pManaged(preset: ValidationPreset | undefined): boolean {
  return preset?.defaults?.p2p_managed === true;
}

export function presetLocalContainerDir(preset: ValidationPreset | undefined): string | undefined {
  const dir = preset?.defaults?.local_container_dir;
  return typeof dir === "string" && dir.trim() ? dir.trim() : undefined;
}

export function buildValidationPresetRequest(args: {
  presetId: string;
  scope: ValidationScope;
  nodes?: Iterable<string>;
  racks?: Iterable<string>;
  target?: string;
  logDir?: string;
  storage: ValidationStorage;
  rsyncToLocal: boolean;
  campaign?: string;
  campaignVersion?: string;
}): ValidationPresetRequest {
  const sort = (values?: Iterable<string>) =>
    values
      ? [...new Set([...values].map((v) => v.trim()).filter(Boolean))].sort((a, b) =>
          a.localeCompare(b, undefined, { numeric: true }),
        )
      : [];
  const nodes = sort(args.nodes);
  const racks = sort(args.racks);
  const request: ValidationPresetRequest = {
    preset_id: args.presetId,
    scope: args.scope,
    storage: args.storage,
    rsync_to_local: args.rsyncToLocal,
  };
  if (nodes.length > 0) request.nodes = nodes;
  if (racks.length > 0) request.racks = racks;
  const target = args.target?.trim();
  if (target) request.target = target;
  const logDir = args.logDir?.trim();
  if (logDir) request.log_dir = logDir;
  const campaign = args.campaign?.trim();
  if (campaign) request.campaign = campaign;
  // Sent alongside the id because the id alone cannot detect an edited roster: it is a
  // label a human chose, and a launch under a stable id used to run whatever the roster
  // currently held. The version is a digest of the nodes, so a page holding a stale list
  // is refused instead of silently validating different hardware.
  const campaignVersion = args.campaignVersion?.trim();
  if (campaignVersion) request.campaign_version = campaignVersion;
  return request;
}

export function formatPresetRequirements(requirements: ValidationPreset["requirements"]): string[] {
  if (!requirements) return [];
  if (Array.isArray(requirements)) return requirements.map(String);
  return Object.entries(requirements).flatMap(([key, value]) => {
    if (value === null || value === undefined) return [];
    const label = key.replaceAll("_", " ");
    if (typeof value === "boolean") return [value ? label : `no ${label}`];
    if (Array.isArray(value)) return [`${label}: ${value.join(", ")}`];
    return [`${label}: ${String(value)}`];
  });
}
