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
import { apiFetch } from "../lib/apiFetch";
import type {
  ActiveContextResponse,
  ActiveSelection,
  CatalogOverview,
  CatalogVocab,
  Platform,
  Project,
  Tool,
} from "../types/catalog";

async function apiJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(url, init);
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    const errs = Array.isArray(data?.errors) ? data.errors.join("; ") : undefined;
    throw new Error(errs || data?.error || `HTTP ${res.status}`);
  }
  return data as T;
}

const jsonInit = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

// ---- Overview / vocab / active ------------------------------------------

export const getOverview = () => apiJson<CatalogOverview>("/api/catalog/overview");

export const getVocab = () => apiJson<CatalogVocab>("/api/catalog/vocab");

export const getActiveContext = () => apiJson<ActiveContextResponse>("/api/catalog/active");

export async function setActiveContext(sel: ActiveSelection): Promise<ActiveSelection> {
  const data = await apiJson<{ active: ActiveSelection }>(
    "/api/catalog/active",
    jsonInit("PUT", sel),
  );
  return data.active;
}

// ---- Platforms -----------------------------------------------------------

export async function listPlatforms(): Promise<Platform[]> {
  return (await apiJson<{ items: Platform[] }>("/api/catalog/platforms")).items;
}

export async function savePlatform(p: Partial<Platform>): Promise<Platform> {
  const method = p.id ? "PUT" : "POST";
  const url = p.id ? `/api/catalog/platforms/${encodeURIComponent(p.id)}` : "/api/catalog/platforms";
  return (await apiJson<{ item: Platform }>(url, jsonInit(method, p))).item;
}

export const deletePlatform = (id: string) =>
  apiJson<{ deleted: string }>(`/api/catalog/platforms/${encodeURIComponent(id)}`, { method: "DELETE" });

// ---- Tools ---------------------------------------------------------------

export async function listTools(clusterId?: string, surface?: string): Promise<Tool[]> {
  const params = new URLSearchParams();
  if (clusterId) params.set("cluster", clusterId);
  if (surface) params.set("surface", surface);
  const qs = params.toString();
  return (await apiJson<{ items: Tool[] }>(`/api/catalog/tools${qs ? `?${qs}` : ""}`)).items;
}

export async function saveTool(t: Partial<Tool>): Promise<Tool> {
  const method = t.id ? "PUT" : "POST";
  const url = t.id ? `/api/catalog/tools/${encodeURIComponent(t.id)}` : "/api/catalog/tools";
  return (await apiJson<{ item: Tool }>(url, jsonInit(method, t))).item;
}

export const deleteTool = (id: string) =>
  apiJson<{ deleted: string }>(`/api/catalog/tools/${encodeURIComponent(id)}`, { method: "DELETE" });

// ---- Projects ------------------------------------------------------------

export async function listProjects(): Promise<Project[]> {
  return (await apiJson<{ items: Project[] }>("/api/catalog/projects")).items;
}

export async function saveProject(p: Partial<Project>): Promise<Project> {
  const method = p.id ? "PUT" : "POST";
  const url = p.id ? `/api/catalog/projects/${encodeURIComponent(p.id)}` : "/api/catalog/projects";
  return (await apiJson<{ item: Project }>(url, jsonInit(method, p))).item;
}

export const deleteProject = (id: string) =>
  apiJson<{ deleted: string }>(`/api/catalog/projects/${encodeURIComponent(id)}`, { method: "DELETE" });

// ---- Board bundles -------------------------------------------------------

/** One item's outcome from an import. `skipped` is usually a missing permission. */
export interface BoardImportResult {
  key: string;
  status: "applied" | "skipped" | "failed";
  detail: string;
}

/**
 * Apply the approved items of a board bundle.
 *
 * One request rather than a `saveTool` per item, because the permission to *import* a shared
 * board is its own thing (`catalog.board:import`) and the server decides per item whether the
 * caller may create or overwrite. Writing item-by-item from here would gate on tool-editing
 * permissions alone and leave no record of the import as a single act.
 */
export async function importBoardBundle(
  bundle: unknown,
  approve: string[],
): Promise<{ applied: number; results: BoardImportResult[] }> {
  return apiJson("/api/boards/import", jsonInit("POST", { bundle, approve }));
}
