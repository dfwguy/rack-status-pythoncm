/* SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 */

/**
 * Client for `/api/identity` — user, group and membership administration.
 *
 * Every call here is privileged, and two of them (`addMember`, `resetCredential`) can confer or
 * seize access. The server is the authority on all of it; this module exists to keep the views free
 * of fetch plumbing, not to make decisions.
 *
 * **The server's error text is surfaced verbatim rather than replaced.** Its refusals are written to
 * be read by an operator — "you cannot disable your own account", "refusing to delete the last
 * account that can sign in", "this login was retired and cannot be reused" — and each explains a
 * constraint a generic "Request failed" would hide, leaving the operator to guess which rule they
 * hit.
 */

import { apiFetch } from "../lib/apiFetch";
import type { IdentityGroup, IdentityUser, IssuedCredential } from "../types/identity";

async function apiJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await apiFetch(url, init);
  let data: Record<string, unknown> = {};
  try {
    data = await res.json();
  } catch {
    // A non-JSON body from a privileged route means something upstream answered instead — a proxy
    // error page, say. Falling through to the status line is more honest than inventing a reason.
  }
  if (!res.ok || data.ok === false) {
    throw new Error(String(data.error || `${res.status} ${res.statusText}`));
  }
  return data as T;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

// --- users ---------------------------------------------------------------------------------

export async function listUsers(): Promise<IdentityUser[]> {
  const data = await apiJson<{ users: IdentityUser[] }>("/api/identity/users");
  return data.users ?? [];
}

/** Create a user. The returned password is shown once and is then unrecoverable. */
export async function createUser(
  login: string,
  displayName?: string,
): Promise<IssuedCredential> {
  const data = await apiJson<{ user: { login: string }; password: string }>(
    "/api/identity/users",
    json({ login, display_name: displayName ?? "" }),
  );
  return { login: data.user.login, password: data.password };
}

export async function updateDisplayName(
  login: string,
  displayName: string,
): Promise<IdentityUser> {
  const data = await apiJson<{ user: IdentityUser }>(`/api/identity/users/${encodeURIComponent(login)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name: displayName }),
  });
  return data.user;
}

export async function setDisabled(login: string, disabled: boolean): Promise<IdentityUser> {
  const data = await apiJson<{ user: IdentityUser }>(
    `/api/identity/users/${encodeURIComponent(login)}/disabled`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ disabled }),
    },
  );
  return data.user;
}

/**
 * Issue a new one-time password, seizing the account (FR-13a).
 *
 * Revokes the user's sessions server-side, so whoever was signed in stops being signed in.
 */
export async function resetCredential(login: string): Promise<IssuedCredential> {
  const data = await apiJson<{ password: string }>(
    `/api/identity/users/${encodeURIComponent(login)}/credential`,
    { method: "POST" },
  );
  return { login, password: data.password };
}

/** Delete a user. **Retires the login permanently** — it can never be created again (FR-2b). */
export async function deleteUser(login: string): Promise<void> {
  await apiJson(`/api/identity/users/${encodeURIComponent(login)}`, { method: "DELETE" });
}

// --- groups --------------------------------------------------------------------------------

export async function listGroups(): Promise<IdentityGroup[]> {
  const data = await apiJson<{ groups: IdentityGroup[] }>("/api/identity/groups");
  return data.groups ?? [];
}

export async function createGroup(name: string, description?: string): Promise<void> {
  await apiJson("/api/identity/groups", json({ name, description: description ?? "" }));
}

export async function updateGroupDescription(
  name: string,
  description: string,
): Promise<void> {
  await apiJson(`/api/identity/groups/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ description }),
  });
}

export async function deleteGroup(name: string): Promise<void> {
  await apiJson(`/api/identity/groups/${encodeURIComponent(name)}`, { method: "DELETE" });
}

// --- membership ----------------------------------------------------------------------------
//
// The highest-privilege pair on this surface: membership is how grants are conferred, so whoever
// edits it can grant themselves whatever the group has.

export async function addMember(group: string, login: string): Promise<void> {
  await apiJson(`/api/identity/groups/${encodeURIComponent(group)}/members`, json({ login }));
}

export async function removeMember(group: string, login: string): Promise<void> {
  await apiJson(
    `/api/identity/groups/${encodeURIComponent(group)}/members/${encodeURIComponent(login)}`,
    { method: "DELETE" },
  );
}
