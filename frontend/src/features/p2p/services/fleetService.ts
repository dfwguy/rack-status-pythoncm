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
};

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

export type OperationRequest = {
  action: OperationAction;
  sus: number[];
  /** Required for persist/distribute/repair; ignored by the others. */
  images?: string[];
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
