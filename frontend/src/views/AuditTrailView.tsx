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
 * Observability → Audit Trail.
 *
 * The security record: who did what, to which rack, when, and what came of it. Written since
 * Phase 1 and unreadable until now except by reaching the host.
 *
 * **Three things here are correctness, not presentation:**
 *
 * 1. **A collapsed event shows its count.** The flood guard folds repeated refusals into one line
 *    carrying `count`. Rendering it as a single row would report one refusal where forty-seven
 *    happened, in exactly the review where the count *is* the finding.
 * 2. **"No events match" and "we no longer hold that period" must not look alike.** Retention is
 *    finite, so an empty result for last month is ambiguous unless the view says how far back the
 *    trail reaches. `oldest_held` is a fact from the server, never computed here.
 * 3. **A truncated answer says so.** A filtered scan stops after 20,000 events, so a short page
 *    can mean "we stopped looking" rather than "that is all there was".
 *
 * It does **not** poll. The server's 50 ms budget assumes an on-demand read, and a background
 * timer would also fill the trail with this view's own reads — which are themselves audited.
 */

import { useCallback, useEffect, useState } from 'react';

import Table from '@mui/material/Table';
import TableBody from '@mui/material/TableBody';
import TableCell from '@mui/material/TableCell';
import TableHead from '@mui/material/TableHead';
import TableRow from '@mui/material/TableRow';
import TextField from '@mui/material/TextField';
import { Banner, Button, Card, Flex, Skeleton, Tag, Text } from '@nvidia/foundations-react-core';
import { RefreshCw } from 'lucide-react';

import { defaultSince, readTrail } from '../services/auditTrailService';
import type { AuditEvent, AuditFilter, AuditPage } from '../types/auditTrail';

const FILTERS: { key: keyof AuditFilter; label: string; placeholder: string }[] = [
  { key: 'principal', label: 'Principal', placeholder: 'user:rkoike' },
  { key: 'action', label: 'Action', placeholder: 'rack.power:off' },
  { key: 'resource', label: 'Resource', placeholder: 'p17-r01' },
  { key: 'decision', label: 'Decision', placeholder: 'allow / deny' },
  { key: 'result', label: 'Result', placeholder: 'success / refused' },
];

function decisionTone(event: AuditEvent): 'green' | 'red' | 'gray' {
  if (event.decision === 'deny' || event.result === 'refused') return 'red';
  if (event.decision === 'allow') return 'green';
  return 'gray';
}

/** Local time, because an operator correlating with a cluster incident thinks in local time. */
function when(ts: string): string {
  const parsed = new Date(ts);
  return Number.isNaN(parsed.getTime()) ? ts : parsed.toLocaleString();
}

export default function AuditTrailView() {
  const [page, setPage] = useState<AuditPage | null>(null);
  const [filter, setFilter] = useState<AuditFilter>({ since: defaultSince() });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(
    async (cursor = '') => {
      setLoading(true);
      setError('');
      try {
        const next = await readTrail(filter, { cursor, limit: 50 });
        setPage((previous) =>
          cursor && previous
            ? { ...next, events: [...previous.events, ...next.events] }
            : next,
        );
      } catch (caught) {
        // Surfaced, never swallowed: an empty table and a failed request look identical to an
        // operator, and the wrong conclusion — "nothing happened" — is the dangerous one.
        setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        setLoading(false);
      }
    },
    [filter],
  );

  useEffect(() => {
    void load();
  }, [load]);

  const events = page?.events ?? [];

  return (
    <Flex style={{ flexDirection: "column", gap: 16, padding: 16 }}>
      <Flex style={{ justifyContent: "space-between", alignItems: "center" }}>
        <Text kind="title/lg">Audit Trail</Text>
        <Button
          kind="secondary"
          size="small"
          onClick={() => void load()}
          aria-label="Refresh the audit trail"
        >
          <RefreshCw size={14} /> Refresh
        </Button>
      </Flex>

      <Text kind="body/regular/sm" style={{ opacity: 0.75 }}>
        Every audited action, newest first. Reading this page is itself recorded.
      </Text>

      <Card>
        <Flex style={{ gap: 12, flexWrap: "wrap", padding: 12 }}>
          {FILTERS.map((entry) => (
            <TextField
              key={entry.key}
              size="small"
              label={entry.label}
              placeholder={entry.placeholder}
              value={filter[entry.key] ?? ''}
              onChange={(e) => setFilter({ ...filter, [entry.key]: e.target.value })}
            />
          ))}
          <TextField
            size="small"
            type="datetime-local"
            label="Since"
            /* Defaults to 7 days rather than all time: retention is ~90 days and a filtered scan
               stops after 20,000 events, so an unbounded default would make the ordinary case the
               expensive one. A `since` bound also stops the server's walk instead of just
               excluding rows. */
            value={(filter.since ?? '').slice(0, 16)}
            onChange={(e) =>
              setFilter({
                ...filter,
                since: e.target.value ? new Date(e.target.value).toISOString() : '',
              })
            }
            slotProps={{ inputLabel: { shrink: true } }}
          />
        </Flex>
      </Card>

      {error && <Banner status="error">{error}</Banner>}

      {page?.cursor_expired && (
        <Banner status="warning">
          The page you were reading has been rotated out of the trail; showing the newest events
          instead.
        </Banner>
      )}

      {page?.truncated && (
        <Banner status="info">
          Stopped after scanning {page.scanned_events.toLocaleString()} events — there may be more
          matches further back. Narrow the time range to search deeper.
        </Banner>
      )}

      {page && page.skipped_lines > 0 && (
        <Banner status="warning">
          {page.skipped_lines} unreadable line(s) skipped. A truncated final entry is normal after
          an unclean shutdown.
        </Banner>
      )}

      {loading && !page && <Skeleton style={{ height: 240 }} />}

      {page && events.length === 0 && !loading && (
        /* The two empty states are deliberately different sentences. "No events match" and "we no
           longer hold that period" are different facts, and conflating them would let a reader
           conclude nothing happened when the record has simply aged out. */
        <Banner status="info">
          {page.oldest_held
            ? `No events match. The trail reaches back to ${when(page.oldest_held)}; anything older has been rotated away.`
            : 'The audit trail is empty — no audited action has been recorded yet.'}
        </Banner>
      )}

      {events.length > 0 && (
        <Card>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>When</TableCell>
                <TableCell>Principal</TableCell>
                <TableCell>Action</TableCell>
                <TableCell>Resource</TableCell>
                <TableCell>Decision</TableCell>
                <TableCell>Result</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {events.map((event) => (
                <TableRow key={event.event_id}>
                  <TableCell>
                    <Text kind="mono/sm">{when(event.ts)}</Text>
                  </TableCell>
                  <TableCell>{event.principal}</TableCell>
                  <TableCell>
                    <Text kind="mono/sm">{event.action}</Text>
                    {/* One line can stand for many events. Showing it as a single row would
                        under-report a burst of refusals by up to the flood threshold. */}
                    {event.collapsed && event.count ? (
                      <Tag color="yellow" kind="outline" readOnly density="compact">
                        ×{event.count}
                      </Tag>
                    ) : null}
                  </TableCell>
                  <TableCell>
                    <Text kind="mono/sm">{event.resource}</Text>
                    {/* The principal an attachment was granted to. Without it the trail says a
                        policy was attached and never to whom. */}
                    {event.target ? (
                      <Text kind="body/regular/sm" style={{ opacity: 0.75 }}>
                        {' → '}
                        {event.target}
                      </Text>
                    ) : null}
                  </TableCell>
                  <TableCell>
                    <Tag color={decisionTone(event)} kind="solid" readOnly density="compact">
                      {event.decision}
                    </Tag>
                  </TableCell>
                  <TableCell>{event.result}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}

      {page && page.cursor && events.length > 0 && (
        <Flex style={{ justifyContent: "space-between", alignItems: "center" }}>
          <Text kind="body/regular/sm" style={{ opacity: 0.7 }}>
            {events.length} event(s) shown
            {page.oldest_held ? ` · trail reaches back to ${when(page.oldest_held)}` : ''}
          </Text>
          <Button
            kind="tertiary"
            size="small"
            disabled={loading}
            onClick={() => void load(page.cursor)}
          >
            Load older
          </Button>
        </Flex>
      )}
    </Flex>
  );
}
