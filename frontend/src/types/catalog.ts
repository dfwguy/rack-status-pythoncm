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

/**
 * Expected scale-out fabric shape, and the Scalable Unit it bounds.
 *
 * The SU lives here rather than in geometry because the fabric is what limits it: an SU is
 * the racks one set of leaf and spine switches serves, so its size is a property of the
 * platform. `racks_per_su: 0` means this platform has no SU model — draw ungrouped racks.
 */
export interface PlatformFabric {
  scaleout: string;
  rails_per_gpu: number;
  rail_rate_gbps: number;
  ns_ifname: string;
  racks_per_su: number;
  leaf_switches_per_su: number;
  spine_switches_per_su: number;
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
  fabric: PlatformFabric;
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

/**
 * One Scalable Unit and the racks in it.
 *
 * Empty `scalable_units` on a cluster means "derive from the platform's
 * `fabric.racks_per_su`" — it does not mean the cluster has one SU. A platform with
 * `racks_per_su: 0` has no SU model at all, and the UI must draw ungrouped racks
 * rather than invent a single SU containing everything.
 */
export interface ClusterScalableUnit {
  id: string;
  label: string;
  racks: string[];
}

/**
 * A pinned roster of exact compute nodes, for a reproducible acceptance run.
 *
 * There is no `version` field on purpose: the version is a digest the backend derives from
 * `nodes`, so it cannot drift from the roster it describes. Edit the nodes and the version
 * changes by itself.
 */
export interface ClusterCampaign {
  id: string;
  label: string;
  nodes: string[];
  /** Exact nodes required per rack, or 0 to not enforce a count. */
  nodes_per_rack: number;
  created_at: string;
  notes: string;
}

/**
 * Where an external collector writes this cluster's evidence, if it has one.
 *
 * Empty is the common case: most clusters' evidence is the logs this product reads itself.
 */
export interface ClusterEvidence {
  snapshot_path: string;
  healthcheck_root: string;
}

/**
 * How this site's compute fabric is wired and addressed, where that differs from its platform.
 *
 * Every field is an override; blank means the on-node scan (`bin/scripts/lib/fabric.inc.sh`)
 * detects it. NscaleTX-NC needs none of them -- the scan finds its SR-IOV RoCE rails
 * (`roce_vf_r1..4`) and their IPv6-only GIDs on its own -- but a site where detection is wrong
 * states the answer here, per cluster, instead of editing a script.
 */
export interface ClusterFabric {
  /** "" inherits the platform's `fabric.scaleout`; otherwise "ib" or "roce". */
  scaleout: string;
  /** "" reads it off the compute GIDs; otherwise "ipv4" or "ipv6" (NCCL_IB_ADDR_FAMILY). */
  addr_family: string;
  /** An NCCL_IB_HCA value naming the compute plane, e.g. "roce_vf_r"; "" detects. */
  compute_hca: string;
}

export interface ClusterConnection {
  exec_mode: string;
  ssh_host: string;
  ssh_user: string;
  ssh_key: string;
  remote_base_path: string;
  // Where this cluster's nodes write the logs Rack Status ingests. Per-cluster so each project
  // reads its own cluster's logs; supersedes the deprecated global RACK_STATUS_LOG_DIR env var.
  log_dir: string;
  // Where this cluster keeps its container images: the shared store the launch host reads, and
  // the per-node cache jobs run from. Mount points, so per-cluster; blank = deployment default.
  shared_container_dir: string;
  node_container_dir: string;
}

export interface Cluster {
  id: string;
  name: string;
  platform_id: string;
  /** Who manages the machines (bcm/maas). */
  cluster_manager: string;
  /** Where work runs (slurm/pythoncm/...). Distinct from cluster_manager. */
  scheduler: string;
  /**
   * How nodes map to the floor. "direct": the rack id is embedded in the hostname and parsed
   * via the naming templates. "translate": the hostname does not encode the rack — rack/slot
   * come from the cluster-manager provider and the SU grouping from `scalable_units`.
   * "pool": there is no placement data anywhere — the fleet renders as a flat list of nodes
   * (the Akamai/Reno RTX 6000 case). Node shape (GPUs per node) still comes from the scheduler.
   */
  rack_mapping_mode: "direct" | "translate" | "pool";
  site_prefix: string;
  naming: ClusterNaming;
  geometry: ClusterGeometry;
  /** Per-site fabric overrides; all blank = detect on the node. */
  fabric: ClusterFabric;
  connection: ClusterConnection;
  /** Empty means "derive from the platform's `fabric.racks_per_su`", not "one SU". */
  scalable_units: ClusterScalableUnit[];
  /** Pinned acceptance rosters; a site may keep one per bring-up wave. */
  campaigns: ClusterCampaign[];
  /** External evidence feed paths; empty when the cluster has no collector. */
  evidence: ClusterEvidence;
  /**
   * Whether NVIS-P2P (artifact distribution) is enabled for this cluster. P2P is single-tenant —
   * one Dragonfly instance per cluster — so this is a per-cluster capability flag. When on, the
   * P2P nav group and gateway are active for this cluster; when off (default), they are hidden.
   */
  p2p_enabled?: boolean;
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
  schedulers: string[];
  tool_categories: string[];
  exec_modes: string[];
  exec_targets: string[];
  statuses: string[];
}
