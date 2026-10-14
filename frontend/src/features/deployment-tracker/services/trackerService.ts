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
import { apiDownload, apiFetch } from "../../../lib/apiFetch";
import type { DeploymentTrackerResponse } from "../types";

export const TRACKER_URL = "/api/deployment-tracker";
export const TRACKER_SITE_URL = "/api/deployment-tracker/site.html";

/** The tracker dataset. A 503 means the node inventory is still loading; the caller retries. */
export async function fetchDeploymentTracker(): Promise<DeploymentTrackerResponse> {
  const res = await apiFetch(TRACKER_URL, { cache: "no-store" });
  const text = await res.text();
  let payload: Record<string, unknown>;
  try {
    payload = text ? (JSON.parse(text) as Record<string, unknown>) : {};
  } catch {
    throw new Error(`Invalid JSON (HTTP ${res.status}): ${text.slice(0, 240)}`);
  }
  if (!res.ok || payload.ok === false) {
    throw new Error(typeof payload.error === "string" ? payload.error : `HTTP ${res.status}`);
  }
  return payload as unknown as DeploymentTrackerResponse;
}

/** The static page with today's data in it: one HTML file that opens from disk or uploads to Linkify. */
export function downloadTrackerSite(): Promise<void> {
  return apiDownload(TRACKER_SITE_URL, "deployment-tracker.html");
}
