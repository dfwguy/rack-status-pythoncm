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
import Paper from '@mui/material/Paper';
import TableContainer from '@mui/material/TableContainer';
import TableHead from '@mui/material/TableHead';
import TablePagination from '@mui/material/TablePagination';
import TableRow from '@mui/material/TableRow';
import TextField from '@mui/material/TextField';
import { Banner, Button, Card, Flex, Skeleton, Tag, Text } from '@nvidia/foundations-react-core';
import { Download, RefreshCw } from 'lucide-react';

import {
  ROWS_PER_PAGE_OPTIONS,
  defaultSince,
  downloadExport,
  readTrail,
} from '../services/auditTrailService';
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
  const [rowsPerPage, setRowsPerPage] = useState(50);
  const [pageIndex, setPageIndex] = useState(0);
  /**
   * One cursor per page boundary already crossed, so **Previous** can go back.
   *
   * This is what pagination over an append-only record costs. The other tables hold the whole
   * filtered array and slice it, so a page number is just an offset. The trail is appended at the
   * newest end — exactly where page 1 starts — so an offset shifts between requests and would
   * both repeat and *skip* events. The server therefore pages by `event_id`, and going backwards
   * means remembering the anchors rather than decrementing a number.
   */
  const [cursors, setCursors] = useState<string[]>([]);
  const [exporting, setExporting] = useState(false);

  const load = useCallback(
    async (cursor = '', limit = rowsPerPage) => {
      setLoading(true);
      setError('');
      try {
        setPage(await readTrail(filter, { cursor, limit }));
      } catch (caught) {
        // Surfaced, never swallowed: an empty table and a failed request look identical to an
        // operator, and the wrong conclusion — "nothing happened" — is the dangerous one.
        setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        setLoading(false);
      }
    },
    [filter, rowsPerPage],
  );

  // A filter or page-size change resets to the newest page: any cursor held was an anchor into the
  // *previous* result set, and resuming from it would page through an answer nobody asked for.
  useEffect(() => {
    setCursors([]);
    setPageIndex(0);
    void load('');
  }, [load]);

  const events = page?.events ?? [];
  /* A full page plus a cursor is the only evidence that more exists. A short page is the end, and
   * `truncated` is a different thing entirely -- the scan cap, reported separately, because
   * "there is more we did not look at" is not "there is a next page". */
  const hasMore = Boolean(page?.cursor) && events.length >= rowsPerPage;

  const goToPage = (next: number) => {
    if (next > pageIndex && page?.cursor) {
      setCursors((held) => [...held, page.cursor]);
      setPageIndex(next);
      void load(page.cursor);
      return;
    }
    if (next < pageIndex) {
      const held = cursors.slice(0, next);
      setCursors(held);
      setPageIndex(next);
      void load(held[held.length - 1] ?? '');
    }
  };

  const runExport = async (format: 'json' | 'csv') => {
    setExporting(true);
    setError('');
    try {
      await downloadExport(filter, format);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setExporting(false);
    }
  };

  return (
    <Flex style={{ flexDirection: "column", gap: 16, padding: 16 }}>
      <Flex style={{ justifyContent: "space-between", alignItems: "center" }}>
        <Text kind="title/lg">Audit Trail</Text>
        <Flex style={{ gap: 8 }}>
          {/* Exports the FILTER, not the visible page -- the server re-runs the query, so a
              filtered download is the whole answer rather than the 50 rows on screen. */}
          <Button
            kind="tertiary"
            size="small"
            disabled={exporting}
            onClick={() => void runExport('csv')}
            aria-label="Export the audit trail as CSV"
          >
            <Download size={14} /> CSV
          </Button>
          <Button
            kind="tertiary"
            size="small"
            disabled={exporting}
            onClick={() => void runExport('json')}
            aria-label="Export the audit trail as JSON"
          >
            <Download size={14} /> JSON
          </Button>
          <Button
            kind="secondary"
            size="small"
            onClick={() => void load(cursors[cursors.length - 1] ?? '')}
            aria-label="Refresh the audit trail"
          >
            <RefreshCw size={14} /> Refresh
          </Button>
        </Flex>
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
        /* `TableContainer` over an outlined `Paper`, the same shell NodesView and RackTable use,
           with the pagination *inside* it. It was a KUI `Card` with the controls in a sibling row,
           which read as a panel and a separate strip of buttons rather than one table. */
        <TableContainer component={Paper} variant="outlined">
          <Table size="small" aria-label="Audit trail">
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

          <Flex
            style={{
              justifyContent: 'space-between',
              alignItems: 'center',
              flexWrap: 'wrap',
              paddingLeft: 16,
            }}
          >
            {/* The retention bound sits in the footer beside the pager rather than above the
                table, because it qualifies the rows on screen: an empty page a month back means
                "we no longer hold that", not "nothing happened". FR-10 asks for the oldest event
                actually held, which is a fact the server sends -- a period computed here would be
                a guess, since how far back the trail reaches depends on event rate. */}
            <Text kind="body/regular/sm" style={{ opacity: 0.7 }}>
              {page?.oldest_held ? `Trail reaches back to ${when(page.oldest_held)}` : ''}
            </Text>
            <TablePagination
              component="div"
              /* `-1` while more may follow, an exact total once we are on the last page.
               *
               * MUI derives both arrows' disabled state from `count`, so encoding "is there more"
               * here is all that is needed -- no slot overrides, and no second source of truth for
               * whether Next should work. It is also the honest value: a real total would mean
               * scanning the whole 125 MiB trail on every page turn, and it becomes knowable
               * precisely when the last page arrives. MUI renders "1-50 of more than 50" until
               * then. */
              count={hasMore ? -1 : pageIndex * rowsPerPage + events.length}
              page={pageIndex}
              onPageChange={(_, next) => goToPage(next)}
              rowsPerPage={rowsPerPage}
              onRowsPerPageChange={(e) => setRowsPerPage(parseInt(e.target.value, 10))}
              rowsPerPageOptions={ROWS_PER_PAGE_OPTIONS}
            />
          </Flex>
        </TableContainer>
      )}


    </Flex>
  );
}
