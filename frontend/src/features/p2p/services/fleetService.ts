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
// The operations-API client. Views call these, never `apiFetch` directly, so
// the URL shapes live in one file and a server-side rename is a single edit.
import { apiFetch, apiJson } from "../lib/apiFetch";
import type { FleetStatus, OperationAction, Operation } from "../types/fleet";

// The P2P gateway lives under Rack Status's own `/api/*` surface (a
// `backend/api/p2p_*.py` domain module), reached through the host `apiFetch`.
// The browser never talks to the Dragonfly operations server directly.
const BASE = "/api/p2p";

/** Full fleet snapshot: catalog, coverage, SU readiness, live wave, operations. */
export function getFleetStatus(): Promise<FleetStatus> {
  return apiJson<FleetStatus>(`${BASE}/status`);
}

/** One editable catalog image (a "task"). sha256 may be blank for an image not yet pinned. */
export type CatalogImageEdit = {
  order: number;
  name: string;
  filename: string;
  bytes: number;
  sha256: string;
  required_by: string[];
};

export type CatalogDoc = {
  ok: boolean;
  cluster_id: string;
  /** false when the images are seeded from the current manifest rather than an authored document. */
  authored: boolean;
  images: CatalogImageEdit[];
  /** The names an image's Required by may carry: every launch that stages the catalog image naming it. */
  consumers?: ImageConsumer[];
  /** On save: the copy published to the config/ of the scripts the cluster runs for launchers to check node images by. */
  scripts_catalog?: ScriptsCatalogPublish;
};

/** A launch that runs the catalog image whose Required by lists its ``id`` (a preset or tool). */
export type ImageConsumer = { id: string; label: string };

/** Result of publishing the catalog into the scripts' shared config directory. */
export type ScriptsCatalogPublish = { ok: boolean; dest?: string; images?: number; error?: string };

/** Re-publish the catalog into the config/ of the scripts the cluster runs (for a script tree pushed outside the app). */
export function publishCatalogToScripts(): Promise<ScriptsCatalogPublish> {
  return apiJson<ScriptsCatalogPublish>(`${BASE}/catalog/publish`, { method: "POST" });
}

/** The active cluster's editable catalog (the task list). */
export function getCatalog(): Promise<CatalogDoc> {
  return apiJson<CatalogDoc>(`${BASE}/catalog`);
}

/** Replace the active cluster's catalog. Rejects with an `ApiError` (400) on an invalid document. */
export function saveCatalog(images: CatalogImageEdit[]): Promise<CatalogDoc> {
  return apiJson<CatalogDoc>(`${BASE}/catalog`, {
    method: "PUT",
    body: JSON.stringify({ images }),
  });
}

/** One named roster: a node list an operation can target by name. */
export type RosterEdit = {
  name: string;
  description: string;
  nodes: string[];
};

export type RostersDoc = {
  ok: boolean;
  cluster_id: string;
  rosters: RosterEdit[];
};

/** The active cluster's named rosters. */
export function getRosters(): Promise<RostersDoc> {
  return apiJson<RostersDoc>(`${BASE}/rosters`);
}

/** Replace the active cluster's rosters. Rejects with an `ApiError` (400) on an invalid document. */
export function saveRosters(rosters: RosterEdit[]): Promise<RostersDoc> {
  return apiJson<RostersDoc>(`${BASE}/rosters`, {
    method: "PUT",
    body: JSON.stringify({ rosters }),
  });
}

/**
 * How a distribute turns its roster into waves. `sequential` slices the roster in order;
 * `rack-staged` gives every rack one holder first, then grows every rack up the stage ladder so new
 * nodes pull from rack-mates (see the worker's `rack_staged_waves`).
 */
export type RolloutStrategy = "sequential" | "rack-staged";

/** One saved campaign: a task (catalog image) paired with a roster and optional pacing overrides. */
export type CampaignEdit = {
  name: string;
  description: string;
  /** Catalog image filename (a *.sqsh). */
  image: string;
  /** A named roster, or blank for the cluster's default inventory. */
  roster: string;
  /** Pacing overrides; 0 on a field inherits the cluster's rollout default. */
  wave_size: number;
  parallelism: number;
  poll_seconds: number;
  max_launches: number;
  max_unreachable: number;
  timeout_seconds: number;
  /** Nodes per wave after the first stage of a rack-staged plan; 0 inherits. */
  fill_wave_size: number;
  /** Seconds without download progress before a node is stopped and relaunched; 0 inherits. */
  stall_seconds: number;
  /** Checksum-rate override (MB/s) for a fully written image's verify window; 0 inherits. */
  verify_mb_per_second: number;
  /** Hard limit on one node's download (enforced on the node); 0 inherits. */
  download_timeout_seconds: number;
  /** Wave plan override; blank inherits the cluster's rollout strategy. */
  strategy: "" | RolloutStrategy;
  /** Stage ladder override (e.g. ["1", "50%", "100%"]); empty inherits the cluster's. */
  stages: string[];
};

export type CampaignsDoc = {
  ok: boolean;
  cluster_id: string;
  campaigns: CampaignEdit[];
};

/** The active cluster's saved campaigns. */
export function getCampaigns(): Promise<CampaignsDoc> {
  return apiJson<CampaignsDoc>(`${BASE}/campaigns`);
}

/** Replace the active cluster's campaigns. Rejects with an `ApiError` (400) on an invalid document. */
export function saveCampaigns(campaigns: CampaignEdit[]): Promise<CampaignsDoc> {
  return apiJson<CampaignsDoc>(`${BASE}/campaigns`, {
    method: "PUT",
    body: JSON.stringify({ campaigns }),
  });
}

/** The campaign fields that override the cluster's rollout pacing. */
const CAMPAIGN_PACING_KEYS = [
  "wave_size", "parallelism", "poll_seconds", "max_launches", "max_unreachable", "timeout_seconds",
  "fill_wave_size", "stall_seconds", "verify_mb_per_second", "download_timeout_seconds",
] as const;

/**
 * Launch a saved campaign: a `distribute` operation carrying the campaign's image, roster, and the
 * non-zero pacing overrides, tagged with the campaign name so its runs trace back to it. Reuses the
 * operations enqueue (and its single-flight 409), so a campaign holds no run state of its own.
 */
export function launchCampaign(campaign: CampaignEdit): Promise<Operation> {
  const pacing: RolloutPacing = {};
  for (const key of CAMPAIGN_PACING_KEYS) {
    if (campaign[key] > 0) pacing[key] = campaign[key];
  }
  // The wave plan rides along only when the campaign overrides it; otherwise the cluster's applies.
  if (campaign.strategy) pacing.strategy = campaign.strategy;
  if (campaign.stages?.length) pacing.stages = campaign.stages;
  return startOperation({
    action: "distribute",
    sus: [],
    images: [campaign.image],
    roster: campaign.roster,
    pacing,
    campaign: campaign.name,
  });
}

/** A run's pacing overrides: numeric knobs, plus the wave plan (strategy and stage ladder). */
export type RolloutPacing = {
  [key: string]: number | string | string[] | undefined;
  strategy?: RolloutStrategy;
  stages?: string[];
};

export type OperationRequest = {
  action: OperationAction;
  sus: number[];
  /** Required for persist/distribute/repair; ignored by the others. */
  images?: string[];
  /** A named roster to target; blank/absent targets the cluster's default inventory. */
  roster?: string;
  /** Per-run rollout pacing overrides; keys the cluster default inherits are omitted. */
  pacing?: RolloutPacing;
  /** The campaign that launched this run, if any; tags the operation for history. */
  campaign?: string;
};

/**
 * Start a fleet operation.
 *
 * The server serialises mutations behind a single lock and answers 409 when one
 * is already running. That is a normal outcome an operator needs to see, not an
 * error to retry, so it surfaces as an `ApiError` with status 409 for the caller
 * to render.
 */
export function startOperation(request: OperationRequest): Promise<Operation> {
  return apiJson<Operation>(`${BASE}/operations`, {
    method: "POST",
    body: JSON.stringify({
      action: request.action,
      sus: request.sus,
      images: request.images ?? [],
      roster: request.roster ?? "",
      pacing: request.pacing ?? {},
      campaign: request.campaign ?? "",
    }),
  });
}

/** One operation's log content, in the scheduler job-output dialog's response shape. */
export type OperationLog = {
  ok: boolean;
  content?: string;
  too_large?: boolean;
  size_bytes?: number;
  filename?: string;
  error?: string;
};

/**
 * Fetch an operation's worker log for the viewer dialog.
 *
 * Uses `apiFetch` rather than `apiJson` because a missing log is a normal 404 whose body still
 * carries `{ok:false,error}` the dialog renders -- not an exception to swallow.
 */
export async function fetchOperationLog(opId: string): Promise<OperationLog> {
  const res = await apiFetch(`${BASE}/operations/${encodeURIComponent(opId)}/log`, {
    credentials: "same-origin",
  });
  if (res.status === 401) return { ok: false, error: "Not signed in." };
  return (await res.json()) as OperationLog;
}
