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
import { deriveVerifier } from "../lib/srpClient";
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

/**
 * Create a user with a server-generated password.
 *
 * The returned password exists in that one response and nowhere else — the server keeps only its
 * verifier — so a caller that discards it has created an account nobody can sign in to.
 */
/**
 * The profile a new account is created with. An object rather than a positional list because the
 * server accepts seven attributes now — passing those in order is a transposition waiting to happen,
 * and two adjacent optional strings are exactly the pair a caller gets backwards.
 */
export type NewUserProfile = {
  /** Required by the server so every new account can be notified. */
  email: string;
} & Partial<
  Pick<
    IdentityUser,
    "display_name" | "phone_number" | "given_name" | "family_name" | "zoneinfo" | "locale"
  >
>;

function profileBody(profile: NewUserProfile): Record<string, string> {
  return {
    display_name: profile.display_name ?? "",
    email: profile.email,
    phone_number: profile.phone_number ?? "",
    given_name: profile.given_name ?? "",
    family_name: profile.family_name ?? "",
    zoneinfo: profile.zoneinfo ?? "",
    locale: profile.locale ?? "",
  };
}

export async function createUser(
  login: string,
  profile: NewUserProfile,
): Promise<IssuedCredential> {
  const data = await apiJson<{ user: { login: string }; password: string }>(
    "/api/identity/users",
    json({ login, ...profileBody(profile) }),
  );
  return { login: data.user.login, password: data.password };
}

/**
 * Create a user with a password the operator chose.
 *
 * **The password does not leave this browser.** `deriveVerifier` turns it into a salt and verifier
 * here, and only those are sent — the same property SRP gives a login, extended to enrolment. The
 * server refuses a plaintext `password` field outright rather than hashing it for you, which would
 * put a live credential in a request body in an SRP system.
 *
 * Returns nothing: there is no one-time password to show, because the operator already knows it.
 */
export async function createUserWithPassword(
  login: string,
  password: string,
  profile: NewUserProfile,
): Promise<void> {
  // Derived against the *canonical* login the server will store: SRP hashes the identity into `x`,
  // so deriving against the raw input would produce a verifier that never matches at sign-in — the
  // same failure shape as the change-password bug, reported as "wrong password".
  //
  // The server case-folds, and `toLowerCase()` is equivalent here for one specific reason:
  // `_LOGIN_RE` permits only `[A-Za-z0-9._@+-]` and is validated BEFORE the fold, so every login
  // that reaches it is ASCII, where `casefold` and `toLowerCase` agree. That coupling is not
  // obvious, so `test_identity_admin_api.py` pins the charset — if the regex is ever widened to
  // accept non-ASCII, that test fails rather than this line silently diverging.
  const canonical = login.trim().toLowerCase();
  const { saltHex, verifierHex } = await deriveVerifier(canonical, password);
  await apiJson(
    "/api/identity/users",
    json({
      login: canonical,
      ...profileBody(profile),
      salt: saltHex,
      verifier: verifierHex,
    }),
  );
}

export async function updateDisplayName(
  login: string,
  displayName: string,
): Promise<IdentityUser> {
  return updateProfile(login, { display_name: displayName });
}

/**
 * Change any subset of the editable profile fields.
 *
 * Note what cannot be sent here: `email_verified`. The server refuses it, because asserting an
 * address was verified is a claim relying parties trust for account linking and so gets its own
 * audited action — see {@link setEmailVerified}. Setting a new `email` clears that flag server-side.
 */
export async function updateProfile(
  login: string,
  changes: Partial<
    Pick<
      IdentityUser,
      | "display_name"
      | "email"
      | "phone_number"
      | "given_name"
      | "family_name"
      | "zoneinfo"
      | "locale"
    >
  >,
): Promise<IdentityUser> {
  const data = await apiJson<{ user: IdentityUser }>(`/api/identity/users/${encodeURIComponent(login)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
  return data.user;
}

/** Record (or withdraw) this deployment's assertion that the user's email was verified. */
export async function setEmailVerified(
  login: string,
  verified: boolean,
): Promise<IdentityUser> {
  const data = await apiJson<{ user: IdentityUser }>(
    `/api/identity/users/${encodeURIComponent(login)}/email-verified`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ verified }),
    },
  );
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
