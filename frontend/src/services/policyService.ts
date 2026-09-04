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

/** Client for `/api/policies`. */

import { apiFetch } from '../lib/apiFetch';
import type { PolicyDocument, PolicyRow, PolicyVocabulary } from '../types/policy';

async function json<T>(response: Response, what: string): Promise<T> {
  const body = await response.json().catch(() => ({}) as Record<string, unknown>);
  if (!response.ok || body?.ok === false) {
    // The server's own message, not a generic one. A policy rejection names the field and the
    // rule that refused it, and a delete refusal names every affected subject — paraphrasing
    // either would throw away the only part an operator can act on.
    throw new Error(String(body?.error ?? `${what} failed (${response.status})`));
  }
  return body as T;
}

export async function listPolicies(): Promise<PolicyRow[]> {
  const resp = await apiFetch('/api/policies');
  const body = await json<{ policies: PolicyRow[] }>(resp, 'listing policies');
  return body.policies ?? [];
}

export async function getPolicy(
  id: string,
): Promise<{ policy: PolicyDocument; attached_to: string[] }> {
  const resp = await apiFetch(`/api/policies/${encodeURIComponent(id)}`);
  return json(resp, 'loading the policy');
}

export async function createPolicy(document: PolicyDocument): Promise<PolicyDocument> {
  const resp = await apiFetch('/api/policies', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(document),
  });
  const body = await json<{ policy: PolicyDocument }>(resp, 'creating the policy');
  return body.policy;
}

export async function updatePolicy(
  id: string,
  document: PolicyDocument,
): Promise<PolicyDocument> {
  const resp = await apiFetch(`/api/policies/${encodeURIComponent(id)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(document),
  });
  const body = await json<{ policy: PolicyDocument }>(resp, 'saving the policy');
  return body.policy;
}

/**
 * Delete a policy.
 *
 * `force` is separate and explicit because without it the server refuses while the policy is still
 * attached, and names who is affected. That refusal is the useful path — deleting an attached
 * policy silently changes what somebody can do — so the UI shows the names and asks again rather
 * than forcing on the first click.
 */
export async function deletePolicy(id: string, force = false): Promise<string[]> {
  const query = force ? '?force=true' : '';
  const resp = await apiFetch(`/api/policies/${encodeURIComponent(id)}${query}`, {
    method: 'DELETE',
  });
  const body = await json<{ detached_from: string[] }>(resp, 'deleting the policy');
  return body.detached_from ?? [];
}

export async function attachPolicy(id: string, subject: string): Promise<void> {
  const resp = await apiFetch(`/api/policies/${encodeURIComponent(id)}/attachments`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ subject }),
  });
  await json(resp, 'attaching the policy');
}

export async function detachPolicy(id: string, subject: string): Promise<void> {
  const resp = await apiFetch(
    `/api/policies/${encodeURIComponent(id)}/attachments/${subject}`,
    { method: 'DELETE' },
  );
  await json(resp, 'detaching the policy');
}

export async function policiesForSubject(subject: string): Promise<string[]> {
  const resp = await apiFetch(`/api/policies/subjects/${subject}`);
  const body = await json<{ policies: string[] }>(resp, 'loading attachments');
  return body.policies ?? [];
}

/** The JSON Schema the server enforces, for the editor. See `PolicyEditor`. */
export async function fetchPolicySchema(): Promise<object> {
  const resp = await apiFetch('/api/policies/schema');
  const body = await json<{ schema: object }>(resp, 'loading the policy schema');
  return body.schema;
}

export async function fetchVocabulary(): Promise<PolicyVocabulary> {
  const resp = await apiFetch('/api/policies/vocabulary');
  return json<PolicyVocabulary>(resp, 'loading the action vocabulary');
}
