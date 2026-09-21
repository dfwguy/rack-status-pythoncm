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
/**
 * setupService — the first-run wizard's API client.
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 *
 * Only the parts the catalog API cannot answer live here: the trigger, the naming helper, the
 * key writer and the completion flag. The wizard creates its project through
 * `catalogService.saveProject` / `setActiveContext`, so there is one code path that writes a
 * Project document and one place where its shape is understood.
 */
import { apiFetch } from "../lib/apiFetch";

async function apiJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(url, init);
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    throw new Error(data?.error || `HTTP ${res.status}`);
  }
  return data as T;
}

const post = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export interface SetupStatus {
  required: boolean;
  /** `forced` | `project-configured` | `already-completed` | `no-project-configured`. */
  reason: string;
  forced: boolean;
  projects?: string[];
}

export interface ScriptLayout {
  tree_root: string;
  layered: boolean;
  layers: string[];
  scripts_dir: string;
  var_dir: string;
  project: string;
  platform: string;
  legacy_dir: string;
}

/** One proposed naming triplet, already proved against the operator's own hostname. */
export interface NamingCandidate {
  rack_id_template: string;
  ct_id_template: string;
  nvsw_id_template: string;
  ct_joiner: string;
  node_id_mode: string;
  /** What these templates make of the example — shown so the operator confirms a real parse. */
  parsed: { hostname: string; rack_id: string; tray: string };
  breakdown: { cluster: string; row: string; rack: string };
  rack_digit_groups: number;
  row_is_a_slot: boolean;
}

export interface NamingProposal {
  ok: boolean;
  hostname: string;
  candidates: NamingCandidate[];
  /** Said out loud because one hostname cannot resolve every ambiguity. */
  caveat: string;
}

export const getSetupStatus = () =>
  apiJson<{ setup: SetupStatus; scripts: ScriptLayout }>("/api/setup/status");

export const proposeNaming = (hostname: string) =>
  apiJson<NamingProposal>("/api/setup/naming/propose", post({ hostname }));

/**
 * Store key material and get back the *path* to put in `connection.ssh_key`.
 *
 * The material goes up once and never comes back: the response carries a path and a
 * fingerprint. `Cluster.connection.ssh_key` is a path because that is what `ssh -i` takes.
 */
export const storeSshKey = (project: string, cluster: string, material: string) =>
  apiJson<{ path: string; fingerprint: string }>(
    "/api/setup/ssh-key",
    post({ project, cluster, material }),
  );

export const completeSetup = (project: string) =>
  apiJson<{ setup: SetupStatus }>("/api/setup/complete", post({ project }));

export const reopenSetup = () => apiJson<{ setup: SetupStatus }>("/api/setup/reopen", post({}));

/** Sending the shipped script layers to the head — the step before a project's own copy. */
export interface ScriptDeployResult {
  ok: boolean;
  dry_run: boolean;
  dest: string;
  files: number;
  bytes: number;
  layers: string[];
  chunks: number;
  seconds?: number;
  detail?: string;
  error?: string;
  layout: ScriptLayout;
}

/** What a deploy would send. Answers without contacting the cluster, so it works before SSH does. */
export const previewScriptDeploy = () =>
  apiJson<ScriptDeployResult>("/api/scripts/deploy-preview");

export const deployScripts = () =>
  apiJson<ScriptDeployResult>("/api/scripts/deploy", post({}));

export interface ScriptBootstrapResult {
  project: string;
  platform: string;
  dest: string;
  copied: number;
  differs_from_defaults: number;
  forced: boolean;
  layout: ScriptLayout;
}

export const getProjectScripts = (projectId: string) =>
  apiJson<{
    project: string;
    platform: string;
    layout: ScriptLayout;
    project_dir: string;
    bootstrap_available: boolean;
    would_copy?: number;
    differs_from_defaults?: number;
    dest?: string;
    detail?: string;
  }>(`/api/catalog/projects/${encodeURIComponent(projectId)}/scripts`);

export const bootstrapProjectScripts = (projectId: string, force = false, cluster = "") =>
  apiJson<ScriptBootstrapResult>(
    `/api/catalog/projects/${encodeURIComponent(projectId)}/scripts`,
    post({ force, cluster }),
  );
