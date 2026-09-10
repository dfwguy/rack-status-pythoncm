# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Read the audit trail: newest-first, bounded, and without touching it.

The sink's counterpart. `sink.py` writes an append-only rotating JSONL record; nothing until now
could read it back, so the only way to answer "who did what, to which rack, when" was to reach the
host and `cat` a file (`.plc/briefs/auth-audit-reader-brief.md`).

**Four properties carry the whole design, and three of them are non-obvious.**

# **Newest-first means reverse *insertion* order, never `ts` order.** `ts` is wall-clock, so an NTP
  step can stamp a later-appended event with an earlier time. Sorting by it would silently reorder
  history and — far worse — make a cursor skip events, because the anchor would no longer be where
  the sort put it. The sequence is the physical order of the lines, read backwards.
# **Pagination is by cursor, not offset.** The trail is appended at the newest end, which is exactly
  where page 1 begins, so between two requests an offset shifts by however many events arrived:
  some repeat and some are **skipped**. Skipped events are the worst defect an audit reader can
  have, and nothing about the response would look wrong.
# **The scan is capped, and says when the cap bit.** A filter defeats early exit — `principal=alice`
  with three matches among 267,000 events forces a full walk — so an answer may be incomplete, and
  an incomplete answer that looks complete is worse than a slow one. `Page.truncated` is the
  difference between "alice did nothing" and "we stopped looking".
# **It never writes.** Not the trail, not a temp file, not a "tidy up expired rows while we're here".
  `identity-auth-traps.mdc` §2 is the precedent: a read path that pruned on read had TinyDB rewrite
  the file even when nothing matched, which invalidated the cache keyed on its mtime — so the cache
  never hit once, and every functional test passed throughout.

Rotation runs opposite to intuition: `audit.jsonl` holds the *newest* events and `audit.jsonl.1` is
*older*, so a newest-first walk goes current file → `.1` → `.2`, and within each file from the end.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from audit import sink

#: Read granularity when walking a file backwards. 256 KiB is ~535 events at the measured 490 bytes,
#: so an unfiltered first page of 50 is satisfied by one chunk of one file however much history
#: exists — which is what makes NFR-1's 50 ms budget reachable at a 125 MiB ceiling.
CHUNK_BYTES = 256 * 1024

#: FR-5. Named rather than "bounded", so the cap is testable and NFR-1 has a stated worst case.
MAX_SCAN_EVENTS = 20_000
MAX_SCAN_BYTES = 8 * 1024 * 1024

DEFAULT_PAGE = 50
MAX_PAGE = 500


@dataclass(frozen=True)
class Filter:
    """What narrows a read. Every field is optional; all supplied fields compose as AND."""

    principal: str = ""
    action: str = ""
    service: str = ""
    resource: str = ""
    decision: str = ""
    result: str = ""
    since: str = ""  # ISO 8601, inclusive
    until: str = ""  # ISO 8601, inclusive

    def matches(self, event: dict[str, Any]) -> bool:
        for name in ("principal", "action", "service", "decision", "result"):
            wanted = getattr(self, name)
            if wanted and str(event.get(name, "")) != wanted:
                return False
        # Substring for `resource`, exact for the rest: a resource is often asked about by rack
        # ("p17-r01") rather than by its full `rack.power:p17-r01` identifier, and a fan-out event
        # records `rack.power:p17-r01 (+3 more)`, which no exact match would ever find.
        if self.resource and self.resource not in str(event.get("resource", "")):
            return False
        ts = str(event.get("ts", ""))
        if self.since and ts < self.since:
            return False
        return not (self.until and ts > self.until)

    def is_empty(self) -> bool:
        return not any(
            (self.principal, self.action, self.service, self.resource,
             self.decision, self.result, self.since, self.until)
        )


@dataclass
class Page:
    """One page, plus everything needed to judge whether it is the whole answer."""

    events: list[dict[str, Any]] = field(default_factory=list)
    cursor: str = ""
    #: The scan stopped at the cap with more history unread — so an empty or short page may mean
    #: "we stopped looking", not "nothing more happened". FR-5.
    truncated: bool = False
    #: The cursor's anchor event no longer exists, having been rotated out between requests. A real
    #: state rather than an error: reporting it beats returning an empty page that reads as "no
    #: more events". FR-1a.
    cursor_expired: bool = False
    scanned_events: int = 0
    scanned_bytes: int = 0
    #: NFR-3. A torn final line from a crash mid-write is expected, not exceptional.
    skipped_lines: int = 0
    oldest_held: str = ""
    files: int = 0


def trail_files(path: Path | None = None) -> list[Path]:
    """The trail, newest file first.

    `audit.jsonl`, then `audit.jsonl.1`, `.2`, … — which is *newest to oldest*, the opposite of what
    a sorted directory listing suggests. Only files that exist are returned, so a trail that has
    not yet rotated is a one-element list and an absent trail is empty (FR-11).
    """
    active = path or Path(sink.lc.resolve_path(str(sink.AUDIT_FILE.resolve())))
    found = [active] if active.is_file() else []
    index = 1
    while True:
        rotated = active.with_name(f"{active.name}.{index}")
        if not rotated.is_file():
            break
        found.append(rotated)
        index += 1
    return found


def _lines_backwards(handle, size: int) -> Iterator[tuple[bytes, int]]:
    """Yield `(line, bytes_read)` from the end of an open binary file, newest first.

    Reads fixed chunks and keeps only the partial line straddling a chunk boundary, so peak memory
    is the chunk plus one line rather than the file (NFR-2). The handle is opened once by the
    caller and read through a single descriptor, which is also what makes a rotation mid-read
    harmless: on POSIX the rename does not disturb an already-open file (FR-12).
    """
    position = size
    remainder = b""
    while position > 0:
        read_size = min(CHUNK_BYTES, position)
        position -= read_size
        handle.seek(position)
        chunk = handle.read(read_size) + remainder
        parts = chunk.split(b"\n")
        # The first element may be a partial line continuing into the chunk before it — unless we
        # have reached the start of the file, where it is genuinely the first line.
        remainder = parts.pop(0) if position > 0 else b""
        for line in reversed(parts):
            if line.strip():
                yield line, len(line) + 1
    if remainder.strip():
        yield remainder, len(remainder) + 1


def read(
    flt: Filter | None = None,
    *,
    limit: int = DEFAULT_PAGE,
    cursor: str = "",
    path: Path | None = None,
) -> Page:
    """One page of the trail, newest first.

    `cursor` is the `event_id` of the last event of the previous page; the walk discards events
    until it passes that anchor. Cost is O(pages × limit) to reach page *k*, which is the price of
    correctness under concurrent append — and a `since` bound is the cheap way to reach far back,
    because it *terminates* the walk rather than merely excluding rows.
    """
    return _collect(flt, limit=max(1, min(int(limit), MAX_PAGE)), cursor=cursor, path=path)


def export(flt: Filter | None = None, *, path: Path | None = None) -> Page:
    """Everything matching *flt*, up to the scan cap, in one walk.

    Separate from `read` because the bound differs and only the bound differs. A browsing request is
    clamped to `MAX_PAGE` (500) so one careless caller cannot ask for the whole trail; an export
    *is* the request for the whole trail, bounded instead by `MAX_SCAN_EVENTS`.

    **Expressed as its own entry point rather than a `limit=` a caller may raise.** If `read` took
    the ceiling as an argument, the paging endpoint would inherit the ability to return 20,000 rows
    from a query parameter, and the clamp would be advisory. Two functions over one shared walk
    keeps each bound where it belongs and keeps them from drifting apart.

    Truncation is reported exactly as it is for a page: an export that hit the cap is a *partial*
    answer, and a file that does not say so is worse than a slow download.
    """
    page = _collect(flt, limit=MAX_SCAN_EVENTS, cursor="", path=path)
    if len(page.events) >= MAX_SCAN_EVENTS:
        # Filling the bound is truncation *for an export*, and `_collect` cannot know that: for a
        # page, a full result is ordinary and the cursor says where to resume. An export has no
        # "next", so a full one has silently dropped whatever came before -- and a file claiming to
        # be complete is evidence of the wrong thing. Caught by the export tests, which asserted
        # truncation and got `False`.
        page.truncated = True
    return page


def _collect(
    flt: Filter | None,
    *,
    limit: int,
    cursor: str,
    path: Path | None,
) -> Page:
    """The shared walk. Callers set the bound; this enforces the scan cap regardless."""
    flt = flt or Filter()
    page = Page()
    files = trail_files(path)
    page.files = len(files)
    if not files:
        return page  # FR-11: an absent trail is an empty answer, not an error.

    seeking = bool(cursor)

    for trail_file in files:
        try:
            size = trail_file.stat().st_size
            with open(trail_file, "rb") as handle:
                for raw, consumed in _lines_backwards(handle, size):
                    page.scanned_bytes += consumed
                    try:
                        event = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        page.skipped_lines += 1
                        continue
                    # A JSON *array* or scalar is valid JSON and is not an event. Checked rather
                    # than raised-and-caught: an exception for a type test reads as an error path
                    # when it is an ordinary decision, and `raise ValueError` here also trips
                    # ruff's `type-check-without-type-error`.
                    if not isinstance(event, dict):
                        page.skipped_lines += 1
                        continue

                    page.scanned_events += 1

                    if seeking:
                        # Still walking back to the anchor. Counted against the cap, because the
                        # work is real; skipping it in the accounting would make a deep page look
                        # free and let the cap be evaded by paging.
                        if event.get("event_id") == cursor:
                            seeking = False
                        elif _capped(page):
                            page.truncated = True
                            return _finish(page, files, path)
                        continue

                    # A `since` bound terminates the walk: events are in reverse insertion order,
                    # so once we are older than `since` everything remaining is older still. This
                    # is what keeps a bounded question cheap over unbounded history.
                    if flt.since and str(event.get("ts", "")) < flt.since:
                        return _finish(page, files, path)

                    if flt.matches(event):
                        page.events.append(event)
                        if len(page.events) >= limit:
                            page.cursor = str(event.get("event_id", ""))
                            return _finish(page, files, path)

                    if _capped(page):
                        page.truncated = True
                        return _finish(page, files, path)
        except OSError:
            # A file that vanished mid-walk (rotation past the backup ceiling) is not an error: the
            # remaining files still hold a valid, older answer.
            continue

    if seeking:
        # Walked the whole trail without finding the anchor: it has been rotated away. Restart from
        # the newest page rather than return nothing, and say so (FR-1a).
        # `_collect`, not `read`: re-entering the wrapper would re-clamp to MAX_PAGE, so an export
        # whose anchor had been rotated away would silently return 500 rows and look complete.
        expired = _collect(flt, limit=limit, cursor="", path=path)
        expired.cursor_expired = True
        return expired

    if page.events:
        page.cursor = str(page.events[-1].get("event_id", ""))
    return _finish(page, files, path)


def _capped(page: Page) -> bool:
    return page.scanned_events >= MAX_SCAN_EVENTS or page.scanned_bytes >= MAX_SCAN_BYTES


def _finish(page: Page, files: list[Path], path: Path | None) -> Page:
    """Attach the oldest event held, which is what lets the UI distinguish 'nothing happened' from
    'we no longer hold that period' (FR-10)."""
    if page.events and not page.cursor:
        page.cursor = str(page.events[-1].get("event_id", ""))
    page.oldest_held = _oldest_ts(files)
    return page


def _oldest_ts(files: list[Path]) -> str:
    """The `ts` of the first line of the oldest file.

    A *fact the reader holds*, rather than a retention period computed from the configured budget —
    which would be a guess, since how far back the trail reaches depends entirely on event rate.
    """
    if not files:
        return ""
    oldest = files[-1]
    try:
        with open(oldest, "rb") as handle:
            for raw in handle:
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(event, dict) and event.get("ts"):
                    return str(event["ts"])
                break
    except OSError:
        return ""
    return ""


def stat_snapshot(path: Path | None = None) -> dict[str, tuple[int, int, int]]:
    """`{name: (mtime_ns, size, inode)}` for every trail file.

    Exists for the test that proves a read changes nothing (NFR-4, AC-12). `atime` is deliberately
    absent: a read legitimately updates it, so including it would fail for the wrong reason.
    """
    snapshot: dict[str, tuple[int, int, int]] = {}
    for trail_file in trail_files(path):
        try:
            info = os.stat(trail_file)
        except OSError:
            continue
        snapshot[trail_file.name] = (info.st_mtime_ns, info.st_size, info.st_ino)
    return snapshot
