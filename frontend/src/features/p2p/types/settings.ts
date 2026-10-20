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
// Per-cluster NVIS-P2P instance settings, mirroring backend catalog.models.P2pSettings. Read and
// written whole through /api/p2p/settings; secrets are never carried here (render generates them).

export interface P2pSettingsDoc {
  cluster_id: string;
  install_root: string;
  service_user: string;
  service_group: string;
  service_uid: number;
  service_gid: number;
  /** The cluster's own addresses (no default). */
  manager_ip: string;
  appserver_ip: string;
  /** How the reconcile reaches the P2P app server (its own host, not the cluster control host). */
  appserver_ssh_host: string;
  appserver_ssh_user: string;
  appserver_ssh_key: string;
  console_bind: string;
  /** Absolute path to the eligible-node file (blank = a default under install_root) and the pattern. */
  inventory_source: string;
  inventory_pattern: string;
  container_path: string;
  /** Must name a .json file. */
  catalog_path: string;
  peer_root: string;
  object_store_data_path: string;
  seed_cache_path: string;
  systemd_dir: string;
  /** Campaign defaults — how a staged catalog rollout over this cluster's roster paces itself. */
  rollout_wave_size: number;
  rollout_parallelism: number;
  rollout_poll_seconds: number;
  rollout_max_launches: number;
  rollout_max_unreachable: number;
  rollout_timeout_seconds: number;
  /** How a distribute turns the roster into waves. */
  rollout_strategy: "sequential" | "rack-staged";
  /** Rack-staged ladder: cumulative per-rack targets, counts or shares, ending at "100%". */
  rollout_stages: string[];
  /** Nodes per wave after the first (seed-loading) stage; 0 = the same as rollout_wave_size. */
  rollout_fill_wave_size: number;
  /** Seconds a download may go without writing a byte before it is stopped and relaunched; 0 = off. */
  rollout_stall_seconds: number;
  /** Slowest checksum rate (MB/s) expected on a node: a fully written image's verify window is the larger
   * of 2 x stall and its size at this rate; 0 = flat 2 x stall (default 100). */
  rollout_verify_mb_per_second: number;
  /** Hard limit on one node's download of one image, enforced on the node (default 3600). */
  rollout_download_timeout_seconds: number;
  /** Pull rollouts: where Rack Status writes the agents' desired-state documents (its own view of the
   * directory the origin serves). Blank = pull rollouts not configured. */
  desired_dir: string;
  /** The origin the agents fetch from; blank = http://<appserver_ip>:12130. */
  origin_url: string;
  /** VictoriaMetrics receiving the agents' series; blank = the app's RACK_METRICS_URL. */
  metrics_url: string;
  /** How long a node released by a pull rollout may take before it is deferred. */
  rollout_node_budget_seconds: number;
  /** How many racks one pull rollout drives at the same time. */
  rollout_max_active_racks: number;
}

/** GET /api/p2p/settings and PUT response. */
export interface P2pSettingsResponse extends P2pSettingsDoc {
  ok: boolean;
  /** What is still missing before the reconcile can run (empty = ready to provision). */
  provisioning_blockers: string[];
  /** Image directories the cluster record dictates (Projects → cluster); shown read-only here. */
  image_dirs_from_cluster?: { container_path: boolean; peer_root: boolean };
}

/** POST /api/p2p/provision (202) response. */
export interface P2pProvisionResponse {
  ok: boolean;
  run_id: string;
  state?: string;
  poll: string;
  message?: string;
}
