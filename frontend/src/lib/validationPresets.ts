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
/** Validated recipe ids exposed by GET /api/validation-presets. */
export type ValidationPresetId =
  | "nccl-2.29.3"
  | "llama31-405b-fp8"
  | "gpt-oss-cpu"
  | "exemplar-system-info"
  | "nemo-26.04.01"
  | "mnubergemm-26.03-v1.17";

export type ValidationScope = "nodes" | "rack" | "multirack";
export type ValidationStorage = "nfs" | "local";

export interface ValidationPreset {
  id: ValidationPresetId;
  label: string;
  description: string;
  kind: string;
  defaults?: Record<string, unknown>;
  requirements?: Record<string, unknown> | string[];
  availability?: {
    available: boolean;
    status?: string;
    reasons?: string[];
    prerequisites?: Array<{
      name: string;
      kind: string;
      path: string;
      present: boolean;
    }>;
  };
}

export interface ValidationPresetsResponse {
  ok: boolean;
  presets?: ValidationPreset[];
  error?: string;
}

export interface ValidationPresetRequest extends Record<string, unknown> {
  preset_id: ValidationPresetId;
  scope: ValidationScope;
  nodes?: string[];
  racks?: string[];
  target?: string;
  log_dir?: string;
  storage: ValidationStorage;
  rsync_to_local: boolean;
  local_container_dir: "/opt/containers";
  campaign?: string;
  /** Opaque digest returned by prepare; launch uses it to reject reviewed-target drift. */
  plan_hash?: string;
}

export interface ValidationPresetPlan {
  preset_id: ValidationPresetId;
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
export const VALIDATION_PRESET_LABELS: Record<ValidationPresetId, string> = {
  "nccl-2.29.3": "NCCL 2.29.3 — NVFTP 8K–32G / 500 iterations",
  "llama31-405b-fp8": "Llama 3.1 405B FP8 — 1024 GPUs / 50 steps",
  "gpt-oss-cpu": "GPT-OSS CPU overhead microbenchmark — one independent job per selected node",
  "exemplar-system-info": "NVIDIA DGXC v26.06.01 system_info",
  "nemo-26.04.01": "NeMo 26.04.01",
  "mnubergemm-26.03-v1.17": "MNUberGEMM 26.03-v1.17",
};

export const LOCAL_CONTAINER_DIR = "/opt/containers" as const;

export function buildValidationPresetRequest(args: {
  presetId: ValidationPresetId;
  scope: ValidationScope;
  nodes?: Iterable<string>;
  racks?: Iterable<string>;
  target?: string;
  logDir?: string;
  storage: ValidationStorage;
  rsyncToLocal: boolean;
  campaign?: string;
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
    local_container_dir: LOCAL_CONTAINER_DIR,
  };
  if (nodes.length > 0) request.nodes = nodes;
  if (racks.length > 0) request.racks = racks;
  const target = args.target?.trim();
  if (target) request.target = target;
  const logDir = args.logDir?.trim();
  if (logDir) request.log_dir = logDir;
  const campaign = args.campaign?.trim();
  if (campaign) request.campaign = campaign;
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
