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

/** Types for `/api/audit-trail`. Mirrors `audit.EVENT_FIELDS` plus the flood-summary pair. */

/** One audit event, as the sink wrote it. */
export interface AuditEvent {
  event_id: string;
  ts: string;
  principal: string;
  auth_method: string;
  service: string;
  action: string;
  variant: string;
  resource: string;
  decision: string;
  policy_engine: string;
  result: string;
  src_ip: string;
  src_ip_trust: string;
  user_agent: string;
  request_id: string;
  cluster: string;
  latency_ms: number | null;

  /**
   * Present only on a flood-guard summary, where one line stands for many events.
   *
   * Optional because the overwhelming majority of events are individual. Rendering a collapsed
   * event without its `count` would report one refusal where forty-seven happened — in exactly
   * the review where the count is the finding.
   */
  count?: number;
  collapsed?: boolean;

  /** Present only on an attachment event: the principal a policy was attached to or detached from. */
  target?: string;
}

/** The response envelope. Everything after `events` exists so a caller can tell a complete
 *  answer from a partial one. */
export interface AuditPage {
  ok: boolean;
  events: AuditEvent[];
  cursor: string;
  /** The scan hit its cap with history unread, so a short page may mean "we stopped looking". */
  truncated: boolean;
  /** The cursor's anchor was rotated away; the newest page was returned instead. */
  cursor_expired: boolean;
  scanned_events: number;
  scanned_bytes: number;
  /** Lines that were not parseable events — a torn final line from a crash mid-write. */
  skipped_lines: number;
  /** `ts` of the oldest event still held. Empty when the trail is empty. */
  oldest_held: string;
  files: number;
}

export interface AuditFilter {
  principal?: string;
  action?: string;
  service?: string;
  resource?: string;
  decision?: string;
  result?: string;
  since?: string;
  until?: string;
}

export interface AuditTrailFields {
  ok: boolean;
  filters: string[];
  default_page: number;
  max_page: number;
  max_scan_events: number;
  max_scan_bytes: number;
}
