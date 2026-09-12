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
 * Types for the NVIS catalog (platforms, tools, projects/clusters) served by
 * the `/api/catalog/*` endpoints. Field names are snake_case to match the Flask
 * JSON (mirrors `backend/catalog/models.py`).
 */

export interface PlatformGeometry {
  gpus_per_node: number;
  nodes_per_tray: number;
  trays_per_rack: number;
  gpus_per_rack: number;
  nvlink_domain: number;
}

/** A hardware/system SKU (GPU + CPU + vendor + topology + geometry). */
export interface Platform {
  id: string;
  name: string;
  generation: string;
  gpu_model: string;
  superchip: string;
  cpu_vendor: string;
  cpu_arch: string;
  system_vendor: string;
  form_factor: string;
  geometry: PlatformGeometry;
  interconnect: Record<string, string>;
  capabilities: string[];
  tool_param_defaults: Record<string, string>;
  status: string;
  description: string;
  source: string;
}

export interface ToolParam {
  key: string;
  label: string;
  description: string;
  default: string;
  required: boolean;
}

export interface ToolCompatibility {
  match: string;
  capabilities: string[];
  platform_ids: string[];
}

/**
 * Which launcher a widget's button uses. The backend owns this set
 * (`catalog.models.LAUNCH_KINDS`) so a document can name a launcher but never a URL we do
 * not serve; `planToolLaunch` in `lib/toolLaunch.ts` is the frontend half.
 */
export type ToolLaunchKind = "validation-preset" | "async-job" | "stream" | "dialog" | "none";

export interface ToolLaunch {
  kind: ToolLaunchKind;
  /** Same-origin `/api/...` path, for `async-job` and `stream`. */
  endpoint: string;
  /** Recipe id in the preset engine, for `validation-preset`. */
  preset_id: string;
  /** Required for `dialog`; elsewhere an options dialog shown before the request goes out. */
  dialog: string;
  /** Target scopes this widget offers; empty means the tool's own `execution.target`. */
  scopes: string[];
  /** Merged into the request the launcher builds. */
  body: Record<string, unknown>;
  confirm: boolean;
  /** Selection size this widget accepts; 0 means unbounded. A surface rule, not the recipe's. */
  min_nodes: number;
  max_nodes: number;
}

/** A runnable script described as a document, mapped to compatible platforms. */
export interface Tool {
  id: string;
  name: string;
  category: string;
  script: string;
  env_key: string;
  params: ToolParam[];
  compatible: ToolCompatibility;
  surfaces: string[];
  execution: Record<string, string>;
  description: string;
  ai_context: string;
  status: string;
  source: string;
  /** Short qualifier under the title — version, scope, duration. */
  subtitle: string;
  /** Button text; empty falls back to the surface default. */
  button_label: string;
  /** Lucide icon name; unknown names fall back to the category default. */
  icon: string;
  /** Card banner override; empty uses the per-category artwork. */
  image: string;
  destructive: boolean;
  /** Default board position. Ties break on name. */
  order: number;
  launch: ToolLaunch;
  /** Present only when fetched via `?cluster=` compatibility filter. */
  compatible_with_platform?: boolean;
}

export interface ClusterNaming {
  rack_id_template: string;
  ct_id_template: string;
  nvsw_id_template: string;
  ct_joiner: string;
  slurm_hostlist: string;
}

export interface ClusterGeometry {
  pods: number;
  rows_per_pod: number;
  racks_per_row: number;
  trays_per_rack: number;
}

export interface ClusterConnection {
  exec_mode: string;
  ssh_host: string;
  ssh_user: string;
  ssh_key: string;
  remote_base_path: string;
}

export interface Cluster {
  id: string;
  name: string;
  platform_id: string;
  cluster_manager: string;
  site_prefix: string;
  naming: ClusterNaming;
  geometry: ClusterGeometry;
  connection: ClusterConnection;
}

export interface Project {
  id: string;
  name: string;
  customer: string;
  description: string;
  clusters: Cluster[];
  source: string;
}

export interface ActiveSelection {
  project: string;
  cluster: string;
}

/** GET /api/catalog/active */
export interface ActiveContextResponse {
  ok: boolean;
  active: ActiveSelection;
  project: Project | null;
  cluster: Cluster | null;
  platform: Platform | null;
  ai_context: string;
}

/** GET /api/catalog/overview */
export interface CatalogOverview {
  ok: boolean;
  platforms: Platform[];
  tools: Tool[];
  projects: Project[];
  active: ActiveSelection;
}

/** GET /api/catalog/vocab */
export interface CatalogVocab {
  ok: boolean;
  gpu_models: string[];
  cpu_vendors: string[];
  system_vendors: string[];
  form_factors: string[];
  cluster_managers: string[];
  tool_categories: string[];
  exec_modes: string[];
  exec_targets: string[];
  statuses: string[];
}
