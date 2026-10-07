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
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Wire types for the operations API (`/nvis/api`). These mirror what
// `services/console-api/server.py` actually serves; keep them in step with
// `status_payload()` rather than with what a view happens to read.

/** A catalog image, plus what the server observed about it on disk and in dfstore. */
export type CatalogImage = {
  order: number;
  name: string;
  filename: string;
  bytes: number;
  sha256: string;
  required_by?: string[];
  /** Present in the canonical directory at exactly the catalog byte count. */
  canonical_present: boolean;
  /** dfstore import receipt fields, or null when the image was never imported. */
  persistent_import: string[] | null;
};

export type Catalog = {
  schema_version?: number;
  catalog_name?: string;
  canonical_directory: string;
  generated_at_utc?: string;
  images: CatalogImage[];
};

/**
 * Per-node catalog coverage.
 *
 * `state` is the server's verdict; `bad_size` and `unverified` are the details
 * that a single state label hides, which is why the heatmap encodes them
 * separately rather than folding them into the fill.
 */
export type NodeCoverage = {
  node: string;
  su: number;
  state: "complete" | "syncing" | "incomplete" | "unavailable" | "unreachable" | "not-eligible";
  present: number;
  verified: number;
  unverified: number;
  missing: number;
  bad_size: number;
  /** Non-zero while a sync is actively transferring on this node. */
  active: number;
  /** filename -> per-image state ("ready" | "missing" | "bad-size" | "present-unverified"). */
  images: Record<string, string>;
  observed_at: string | null;
  sync_state?: "complete" | "pending" | "deferred";
  sync_detail?: string;
};

export type SuCoverage = {
  su: number;
  /** Count of nodes holding exactly N images, keyed by N as a string. */
  distribution: Record<string, number>;
  complete: number;
  syncing: number;
  incomplete: number;
  unavailable: number;
};

export type Coverage = {
  generated_at: string | null;
  required_count: number;
  required_images: string[];
  by_su: SuCoverage[];
  nodes: NodeCoverage[];
};

/** Preflight-derived readiness for one SU, merged with coverage counts. */
export type FleetSu = {
  su: number;
  total: number;
  reachable: number;
  eligible: number;
  peers: number;
  ready: number;
  syncing: number;
  incomplete: number;
  unavailable: number;
  distribution: Record<string, number>;
};

export type SyncWave = {
  id: string;
  state: "running" | "complete" | "complete_with_deferrals";
  total: number;
  complete: number;
  pending: number;
  deferred: number;
  percent: number;
  updated_at: string;
};

export type OperationAction =
  | "preflight"
  | "deploy"
  | "persist"
  | "sync"
  | "distribute"
  | "verify"
  | "repair";

export type Operation = {
  id: string;
  action: OperationAction;
  sus: number[];
  images: string[];
  /** A named roster the operation targeted, or null for the default inventory. */
  roster?: string | null;
  /** The campaign that launched this operation, or null for an ad-hoc run. */
  campaign?: string | null;
  state: "running" | "succeeded" | "failed";
  started_at: string;
  finished_at?: string;
  return_code?: number;
  /** Server-built path to the operation log, already prefixed with /nvis/api. */
  log: string;
};

export type FleetStatus = {
  /**
   * Whether this cluster's Dragonfly instance has been provisioned. False on an enabled-but-bare
   * cluster: the catalog still lists the shipped images, but fleet operations are unavailable and
   * the tiles/table read as greenfield until provisioning writes a live snapshot.
   */
  provisioned: boolean;
  catalog: Catalog;
  object_store_healthy: boolean;
  fleet: FleetSu[];
  coverage: Coverage;
  sync: SyncWave | null;
  /** Filenames sharing an identical SHA-256. Equal byte counts are not duplicates. */
  catalog_exact_duplicates: string[][];
  preflight_generated_at: string | null;
  operations: Operation[];
};
