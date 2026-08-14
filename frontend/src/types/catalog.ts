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
