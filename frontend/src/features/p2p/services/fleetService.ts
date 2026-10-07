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
