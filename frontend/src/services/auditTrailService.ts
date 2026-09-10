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

/** Client for `/api/audit-trail`. */

import { apiFetch } from '../lib/apiFetch';
import type { AuditFilter, AuditPage, AuditTrailFields } from '../types/auditTrail';

async function json<T>(response: Response, what: string): Promise<T> {
  const body = await response.json().catch(() => ({}) as Record<string, unknown>);
  if (!response.ok || (body as { ok?: boolean })?.ok === false) {
    // The server's own message. A refusal here is an authorization refusal, and its wording is
    // deliberately identical whether or not the trail holds anything — paraphrasing it locally
    // could reintroduce the distinction the backend removes on purpose.
    throw new Error(
      String((body as { error?: string })?.error ?? `${what} failed (${response.status})`),
    );
  }
  return body as T;
}

/** Days of history the view asks for by default.
 *
 * Not "everything", and that is a design decision rather than caution. Retention is 125 MiB —
 * roughly 90 days and ~267,000 events — while a filtered scan stops after 20,000. Defaulting to
 * all of it would make the common case the expensive one and put `truncated: true` in front of an
 * operator who never asked a hard question. A `since` bound also *terminates* the server's walk
 * rather than merely excluding rows, so a bounded window is cheap however much history exists.
 */
export const DEFAULT_WINDOW_DAYS = 7;

export function defaultSince(now: Date = new Date()): string {
  const since = new Date(now.getTime() - DEFAULT_WINDOW_DAYS * 24 * 60 * 60 * 1000);
  return since.toISOString();
}

export async function readTrail(
  filter: AuditFilter = {},
  options: { limit?: number; cursor?: string } = {},
): Promise<AuditPage> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filter)) {
    if (value) params.set(key, value);
  }
  if (options.limit) params.set('limit', String(options.limit));
  if (options.cursor) params.set('cursor', options.cursor);

  const query = params.toString();
  const resp = await apiFetch(`/api/audit-trail${query ? `?${query}` : ''}`);
  return json<AuditPage>(resp, 'reading the audit trail');
}

/** What the trail can be filtered by, and the page bounds.
 *
 * Served rather than hard-coded here, for the reason the action vocabulary is: a duplicated list
 * is wrong the first time somebody adds a field, and the failure mode is a filter control that
 * silently does nothing.
 */
export async function fetchTrailFields(): Promise<AuditTrailFields> {
  const resp = await apiFetch('/api/audit-trail/fields');
  return json<AuditTrailFields>(resp, 'loading the trail fields');
}
