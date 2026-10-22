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
import { apiDownload, apiFetch } from "../../lib/apiFetch";
import type {
  Artifact,
  HistoryPage,
  Inventory,
  Preview,
  Readiness,
  RunRequest,
  RunStatus,
} from "./types";

const ROOT = "/api/clusterkit";

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(`${ROOT}${path}`, {
    cache: "no-store",
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  const body = (await response.json()) as T & { ok?: boolean; error?: string };
  if (!response.ok || body.ok === false)
    throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

export const getReadiness = (signal?: AbortSignal) =>
  json<Readiness>("/readiness", { signal });
export const getInventory = async (signal?: AbortSignal) =>
  (await json<{ inventory: Inventory }>("/inventory", { signal })).inventory;
export const previewRun = (request: RunRequest, signal?: AbortSignal) =>
  json<Preview>("/preview", {
    method: "POST",
    body: JSON.stringify(request),
    signal,
  });
// Confirmation is deliberately never retried. The preview UUID is also the recovery key.
export const confirmRun = (token: string) =>
  json<RunStatus>("/runs", {
    method: "POST",
    body: JSON.stringify({ confirm: true, confirmation_token: token }),
  });
export const getRun = (id: string, signal?: AbortSignal) =>
  json<RunStatus>(`/runs/${encodeURIComponent(id)}`, { signal });
export const getHistory = (cursor = "", signal?: AbortSignal) =>
  json<HistoryPage>(
    `/runs?limit=25${cursor ? `&before=${encodeURIComponent(cursor)}` : ""}`,
    { signal },
  );
export const getArtifacts = async (id: string, signal?: AbortSignal) =>
  (
    await json<{ artifacts: Artifact[] }>(
      `/runs/${encodeURIComponent(id)}/artifacts`,
      { signal },
    )
  ).artifacts;
export const downloadArtifact = (id: string, name: string) =>
  apiDownload(
    `${ROOT}/runs/${encodeURIComponent(id)}/artifacts/${name.split("/").map(encodeURIComponent).join("/")}`,
    name.split("/").at(-1) || "artifact.txt",
  );
