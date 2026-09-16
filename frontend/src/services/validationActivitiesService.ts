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
import type { ValidationActivitiesResponse, ValidationActivity } from "../types/api";
import { apiFetch } from "../lib/apiFetch";

export async function fetchValidationActivities(params: {
  logDir: string;
  rack?: string;
  scope?: string;
  tool?: string;
  limit?: number;
}): Promise<ValidationActivity[]> {
  const qs = new URLSearchParams({ log_dir: params.logDir });
  if (params.rack) qs.set("rack", params.rack);
  if (params.scope) qs.set("scope", params.scope);
  if (params.tool) qs.set("tool", params.tool);
  if (params.limit != null) qs.set("limit", String(params.limit));
  const res = await apiFetch(`/api/validation-activities?${qs}`);
  const json = (await res.json()) as ValidationActivitiesResponse;
  if (!res.ok || !json.ok || !Array.isArray(json.activities)) {
    throw new Error(json.error || `Validation activities failed (HTTP ${res.status})`);
  }
  return json.activities;
}

export async function dismissValidationActivity(activityId: string): Promise<boolean> {
  const res = await apiFetch(`/api/validation-activities/${encodeURIComponent(activityId)}`, { method: "DELETE" });
  const json = (await res.json()) as { ok?: boolean };
  return Boolean(json.ok);
}
