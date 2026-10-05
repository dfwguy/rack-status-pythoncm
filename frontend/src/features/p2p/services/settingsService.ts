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
// The P2P settings + provisioning client. Views call these, never apiFetch directly, so the URL
// shapes live in one place. Everything goes through the host apiFetch (operator headers, session
// renewal) via the feature's apiJson shim -- the browser never talks to Dragonfly Manager.
import { apiJson } from "../lib/apiFetch";
import type { P2pProvisionResponse, P2pSettingsDoc, P2pSettingsResponse } from "../types/settings";

const BASE = "/api/p2p";

/** The active cluster's P2P settings (defaulted when never configured). */
export function getSettings(): Promise<P2pSettingsResponse> {
  return apiJson<P2pSettingsResponse>(`${BASE}/settings`);
}

/** Save the active cluster's P2P settings; returns the stored document + remaining blockers. */
export function saveSettings(settings: Partial<P2pSettingsDoc>): Promise<P2pSettingsResponse> {
  return apiJson<P2pSettingsResponse>(`${BASE}/settings`, {
    method: "PUT",
    body: JSON.stringify(settings),
  });
}

/**
 * Start (or reconcile) this cluster's Dragonfly control plane.
 *
 * Returns a run id polled at `/api/workflow-runs/<run_id>` like any other workflow run. A 400 with
 * `provisioning_blockers` means the settings are incomplete; the caller surfaces it rather than
 * treating it as an error to retry.
 */
export function provision(): Promise<P2pProvisionResponse> {
  return apiJson<P2pProvisionResponse>(`${BASE}/provision`, { method: "POST", body: "{}" });
}
